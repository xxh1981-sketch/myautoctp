"""Spread CTP vs spread_positions.csv reconcile (mirrors strangle leg_claims check)."""

from __future__ import annotations

from typing import Dict, List, Set, Tuple

from auto_connection import extract_symbol_prefix
from auto_connection_utils import months_match
from auto_position import extract_strike_from_instrument
from spread_close_ledger import build_positions_from_spread_claims
from spread_ledger import store_from_conn


def _spread_symbol_months(spread_tradeinfo: list) -> Set[tuple]:
    keys: Set[tuple] = set()
    for item in spread_tradeinfo or []:
        keys.add((item['future'].lower(), item['month']))
    return keys


def _signed_from_position_row(pos: dict) -> tuple:
    inst = (pos.get('instrument') or pos.get('InstrumentID') or '').strip()
    if not inst:
        return '', 0
    direction = str(pos.get('direction') or pos.get('PosiDirection') or '')
    vol = int(pos.get('volume') or pos.get('Position') or pos.get('position') or 0)
    if vol <= 0:
        return inst, 0
    if direction in ('2', 2, 'LONG'):
        return inst, vol
    if direction in ('3', 3, 'SHORT'):
        return inst, -vol
    return inst, 0


def ctp_spread_signed_claims(
    conn,
    spread_tradeinfo: list,
    positions: list,
    strangle_long_calls: Dict[str, int] = None,
) -> Dict[str, int]:
    """Signed net spread call volume from CTP (long +, short -) on spread tradeinfo.

    ``strangle_long_calls`` (instrument -> long volume) is subtracted from the
    CTP long side so spread reconcile does not double-count the same Call when
    spread and strangle share the same (symbol, month). Pass ``None`` to keep
    the legacy behavior (no subtraction).
    """
    out: Dict[str, int] = {}
    keys = _spread_symbol_months(spread_tradeinfo)
    if not keys:
        return out

    sub = {str(k): int(v) for k, v in (strangle_long_calls or {}).items() if int(v) > 0}

    for sym, month in keys:
        try:
            normalized_month = conn._normalize_month(sym, month)
        except Exception:
            normalized_month = month

        for pos in positions or []:
            inst, signed = _signed_from_position_row(pos)
            if not inst or signed == 0:
                continue
            if extract_symbol_prefix(inst).lower() != sym:
                continue
            if not months_match(inst, month, normalized_month):
                continue
            if extract_strike_from_instrument(inst, normalized_month, option_type='C') is None:
                continue
            if signed > 0 and sub.get(inst, 0) > 0:
                deduct = min(signed, sub[inst])
                signed -= deduct
                sub[inst] -= deduct
                if signed == 0:
                    continue
            out[inst] = out.get(inst, 0) + signed
    return out


def _strangle_long_calls_for_spread(conn) -> Dict[str, int]:
    """Pull strangle long-call leg claims off conn for subtraction (Call only)."""
    from spread_ledger import SpreadLegStore

    runtime = getattr(conn, '_runtime_state', None) or {}
    ledger = runtime.get('_strangle_ledger')
    if ledger is None:
        return {}
    try:
        claims = ledger.list_leg_claims()
    except Exception:
        return {}
    out: Dict[str, int] = {}
    for inst, vol in (claims or {}).items():
        try:
            vol = int(vol)
        except Exception:
            continue
        if vol <= 0:
            continue
        if not SpreadLegStore._is_call_instrument(inst):
            continue
        out[inst] = out.get(inst, 0) + vol
    return out


def ledger_spread_signed_claims(
    conn,
    store,
    spread_tradeinfo: list,
) -> Dict[str, int]:
    """Signed spread claims from SpreadLegStore for configured spread symbols."""
    out: Dict[str, int] = {}
    if store is None:
        return out
    keys = _spread_symbol_months(spread_tradeinfo)
    for sym, month in keys:
        for row in build_positions_from_spread_claims(store, conn, sym, month):
            inst = row['instrument']
            vol = int(row['position'])
            if row.get('direction') == '3':
                out[inst] = out.get(inst, 0) - vol
            else:
                out[inst] = out.get(inst, 0) + vol
    return out


def _previous_halt_state(conn) -> Tuple[bool, List[str]]:
    """Read prior round's halt state for transient fallback."""
    runtime = getattr(conn, '_runtime_state', None) or {}
    prev_halt = bool(runtime.get('_spread_reconcile_halt', False))
    prev_issues = list(runtime.get('_spread_reconcile_issues') or [])
    return prev_halt, prev_issues


SPREAD_TRANSIENT_STREAK_KEY = '_spread_reconcile_transient_streak'
STRANGLE_TRANSIENT_STREAK_KEY = '_strangle_reconcile_transient_streak'


def _transient_halt_threshold(config: dict) -> int:
    dual = config.get('dual_strategy') or {}
    try:
        n = int(dual.get('reconcile_transient_halt_after', 3) or 3)
    except (TypeError, ValueError):
        n = 3
    return max(1, n)


def handle_transient_reconcile_failure(
    conn,
    prev_halt: bool,
    new_issues: List[str],
    prev_issues: List[str],
    config: dict,
    streak_key: str,
    logger,
    log_prefix: str,
) -> Tuple[bool, List[str]]:
    """Track consecutive transient reconcile failures; force halt after N."""
    runtime = getattr(conn, '_runtime_state', None) or {}
    threshold = _transient_halt_threshold(config)
    streak = int(runtime.get(streak_key, 0) or 0) + 1
    runtime[streak_key] = streak
    combined = new_issues + prev_issues

    if streak >= threshold:
        if logger:
            logger.warning(
                f'{log_prefix} 连续 {streak} 次 transient 对账失败，强制 halt'
            )
        return True, combined + [
            f'连续 {streak} 次 transient 对账失败，强制 halt',
        ]

    if logger:
        logger.warning(
            f'{log_prefix} transient 失败 ({streak}/{threshold})，'
            f'沿用上一轮 halt 状态: prev_halt={prev_halt}'
        )
    return prev_halt, combined


def clear_reconcile_transient_streak(conn, streak_key: str) -> None:
    runtime = getattr(conn, '_runtime_state', None)
    if runtime is not None:
        runtime[streak_key] = 0


def _in_grace_window(conn) -> bool:
    """True 表示当前处于 derive/启动后的对账豁免窗口（差异不强制 halt）。"""
    import time as _time

    runtime = getattr(conn, '_runtime_state', None) or {}
    until = runtime.get('_reconcile_grace_until') or 0.0
    try:
        return _time.time() < float(until)
    except (TypeError, ValueError):
        return False


def reconcile_spread_positions(
    conn,
    spread_tradeinfo: list,
    logger,
    config: dict = None,
    positions: list = None,
    trades: list = None,
) -> Tuple[bool, List[str]]:
    """
    Compare spread_positions.csv (SpreadLegStore) to CTP on spread symbols.

    Returns (halt_open, issues). Mismatch blocks spread open/rebalance (not close).

    ``positions`` / ``trades`` may be provided by the caller to share a single
    round-level CTP query between strangle and spread reconcile.

    Transient-vs-真不一致 区分：
      * 查询失败 / SpreadLegStore 未挂载，被视为 transient；
        单次失败沿用上一轮 halt，连续 N 次（``reconcile_transient_halt_after``）
        后强制 halt=True。
      * 仅当对账成功完成且实际比对出仓位差异时才返回 halt=True。
    """
    issues: List[str] = []
    config = config or {}
    dual = config.get('dual_strategy') or {}
    store = store_from_conn(conn)

    if dual.get('auto_sync_spread_positions_csv', True) and store is not None:
        try:
            from spread_fill_sync import sync_csv_from_spread_trades
            sync_csv_from_spread_trades(conn, store, config, logger, trades=trades)
        except Exception as e:
            issues.append(f'spread CSV sync failed: {e}')
            if logger:
                logger.warning(f'[spread-reconcile] CSV sync failed: {e}')

    if positions is None:
        try:
            positions = conn.query_positions_sync(timeout=10) or []
        except Exception as e:
            issues.append(f'position query failed: {e}')
            prev_halt, prev_issues = _previous_halt_state(conn)
            return handle_transient_reconcile_failure(
                conn, prev_halt, issues, prev_issues, config,
                SPREAD_TRANSIENT_STREAK_KEY, logger,
                '[spread-reconcile]',
            )

    if store is None:
        issues.append('SpreadLegStore unavailable')
        prev_halt, prev_issues = _previous_halt_state(conn)
        return handle_transient_reconcile_failure(
            conn, prev_halt, issues, prev_issues, config,
            SPREAD_TRANSIENT_STREAK_KEY, logger,
            '[spread-reconcile]',
        )

    from account_decomposition import external_explains_ctp_ahead, normalize_inst_map

    book = normalize_inst_map(
        ledger_spread_signed_claims(conn, store, spread_tradeinfo),
    )
    strangle_long_calls = (
        _strangle_long_calls_for_spread(conn)
        if dual.get('exclude_strangle_from_spread_reconcile', True)
        else None
    )
    ctp = normalize_inst_map(
        ctp_spread_signed_claims(
            conn, spread_tradeinfo, positions,
            strangle_long_calls=strangle_long_calls,
        ),
    )
    instruments = set(book) | set(ctp)

    halt = False
    for inst in sorted(instruments):
        book_vol = int(book.get(inst, 0))
        ctp_vol = int(ctp.get(inst, 0))
        if ctp_vol == book_vol:
            continue
        if abs(ctp_vol) > abs(book_vol) or (ctp_vol != 0 and book_vol == 0):
            msg = f'{inst}: CTP={ctp_vol} CSV={book_vol} (CTP ahead)'
            if external_explains_ctp_ahead(inst, ctp_vol, book_vol, config):
                msg += ' [已确认外部仓，不 halt]'
                if logger:
                    logger.info(f'[spread-reconcile] {msg}')
                issues.append(msg)
                continue
            halt = True
        else:
            msg = f'{inst}: CSV={book_vol} CTP={ctp_vol} (CSV ahead)'
            halt = True
        issues.append(msg)
        if logger:
            logger.warning(f'[spread-reconcile] {msg}')

    if halt and _in_grace_window(conn):
        if logger:
            logger.warning(
                '[spread-reconcile] 处于豁免窗口 (derive 后)，'
                f'{len(issues)} 条差异仅记录不 halt（待 OnRtnTrade 拉齐）'
            )
        clear_reconcile_transient_streak(conn, SPREAD_TRANSIENT_STREAK_KEY)
        return False, issues

    clear_reconcile_transient_streak(conn, SPREAD_TRANSIENT_STREAK_KEY)
    return halt, issues
