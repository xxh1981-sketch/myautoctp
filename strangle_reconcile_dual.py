"""Dual-strategy reconcile: subtract spread leg claims from strangle CTP compare."""

from __future__ import annotations

from typing import Dict, List, Set, Tuple

from spread_contract_utils import (
    extract_strike_from_instrument,
    months_match,
    symbol_prefix as extract_symbol_prefix,
)
from spread_ledger import store_from_conn
from spread_reconcile import (
    STRANGLE_TRANSIENT_STREAK_KEY,
    clear_reconcile_transient_streak,
    handle_transient_reconcile_failure,
)


def _is_long_option_position(pos: dict) -> bool:
    direction = pos.get('direction') or pos.get('PosiDirection', '')
    return direction in ('2', 2, 'LONG')


def collect_spread_long_call_volumes(
    conn,
    spread_tradeinfo: list,
    positions: list,
) -> Dict[str, int]:
    """
    Legacy heuristic: all long calls on spread tradeinfo symbol+month.

    Prefer spread leg claims via resolve_spread_long_call_volumes().
    """
    out: Dict[str, int] = {}
    if not spread_tradeinfo:
        return out

    for item in spread_tradeinfo:
        symbol = item['future'].lower()
        month = item['month']
        try:
            normalized_month = conn._normalize_month(symbol, month)
        except Exception:
            normalized_month = month

        for pos in positions:
            if not _is_long_option_position(pos):
                continue
            inst = (pos.get('instrument') or pos.get('InstrumentID') or '').strip()
            if not inst:
                continue
            if extract_symbol_prefix(inst).lower() != symbol:
                continue
            if not months_match(inst, month, normalized_month):
                continue
            vol = int(pos.get('volume') or pos.get('Position') or pos.get('position') or 0)
            if vol <= 0:
                continue
            if extract_strike_from_instrument(inst, normalized_month, option_type='C') is None:
                continue
            out[inst] = out.get(inst, 0) + vol
    return out


def resolve_spread_long_call_volumes(
    conn,
    spread_tradeinfo: list,
    positions: list,
    config: dict,
) -> Dict[str, int]:
    """Spread long-call volumes for strangle reconcile (ledger first, optional heuristic)."""
    dual = config.get('dual_strategy') or {}
    if dual.get('use_spread_leg_claims', True):
        store = store_from_conn(conn)
        if store is not None:
            claimed = store.long_call_volumes()
            if claimed or not dual.get('spread_reconcile_fallback_heuristic', False):
                return claimed

    if dual.get('spread_reconcile_fallback_heuristic', False):
        return collect_spread_long_call_volumes(conn, spread_tradeinfo, positions)
    return {}


def _build_ctp_long(trade_symbols: Set[str], positions: list) -> Dict[str, int]:
    ctp_long: Dict[str, int] = {}
    for pos in positions:
        if not _is_long_option_position(pos):
            continue
        inst = (pos.get('instrument') or pos.get('InstrumentID') or '').strip()
        if not inst:
            continue
        sym = extract_symbol_prefix(inst)
        if sym not in trade_symbols:
            continue
        vol = int(pos.get('volume') or pos.get('Position') or pos.get('position') or 0)
        if vol > 0:
            ctp_long[inst] = ctp_long.get(inst, 0) + vol
    return ctp_long


def reconcile_strangle_positions_dual(
    conn,
    ledger,
    trade_symbols: Set[str],
    spread_tradeinfo: list,
    logger,
    config: dict = None,
    positions: list = None,
    trades: list = None,
) -> Tuple[bool, List[str]]:
    """
    Strangle leg_claims vs CTP, excluding spread-owned long calls (leg claims).

    When strangle CSV is empty and remaining long options belong to spread, do not halt.

    ``positions`` / ``trades`` may be pre-fetched by the caller for the round
    so we avoid duplicate CTP queries across strangle + spread reconcile.
    """
    issues: List[str] = []
    config = config or {}
    dual = config.get('dual_strategy') or {}
    exclude_spread = dual.get('exclude_spread_from_strangle_reconcile', True)

    str_cfg = config.get('strangle') or {}
    if str_cfg.get('auto_sync_positions_csv', True):
        try:
            from strangle_fill_sync import sync_csv_from_strangle_trades
            sync_csv_from_strangle_trades(conn, ledger, config, logger, trades=trades)
        except Exception as e:
            issues.append(f'CSV sync failed: {e}')
            if logger:
                logger.warning(f'[reconcile] strangle CSV sync failed: {e}')

    spread_cfg = dual.get('auto_sync_spread_positions_csv', True)
    if exclude_spread and spread_cfg:
        try:
            from spread_fill_sync import sync_csv_from_spread_trades
            store = store_from_conn(conn)
            if store is not None:
                sync_csv_from_spread_trades(conn, store, config, logger, trades=trades)
        except Exception as e:
            issues.append(f'spread CSV sync failed: {e}')
            if logger:
                logger.warning(f'[reconcile] spread CSV sync failed: {e}')

    if positions is None:
        try:
            positions = conn.query_positions_sync(timeout=10) or []
        except Exception as e:
            issues.append(f'position query failed: {e}')
            runtime = getattr(conn, '_runtime_state', None) or {}
            prev_halt = bool(runtime.get('_strangle_reconcile_halt', False))
            prev_issues = list(runtime.get('_strangle_reconcile_issues') or [])
            return handle_transient_reconcile_failure(
                conn, prev_halt, issues, prev_issues, config,
                STRANGLE_TRANSIENT_STREAK_KEY, logger,
                '[reconcile]',
            )

    claimed = {k: int(v) for k, v in ledger.list_leg_claims().items()}
    ctp_long = _build_ctp_long(trade_symbols, positions)
    spread_long = (
        resolve_spread_long_call_volumes(conn, spread_tradeinfo, positions, config)
        if exclude_spread else {}
    )

    halt = False
    for inst, ctp_vol in ctp_long.items():
        book_vol = claimed.get(inst, 0)
        spread_vol = spread_long.get(inst, 0)
        strangle_vol = max(0, ctp_vol - spread_vol)
        if strangle_vol > book_vol:
            gap = strangle_vol - book_vol
            if spread_vol:
                msg = (
                    f'{inst}: strangle_CTP={strangle_vol} CSV={book_vol} '
                    f'(total_CTP={ctp_vol} spread={spread_vol}) gap={gap}'
                )
            else:
                msg = f'{inst}: strangle_CTP={strangle_vol} CSV={book_vol} gap={gap}'
            from account_decomposition import external_explains_strangle_gap
            if external_explains_strangle_gap(inst, gap, config):
                msg += ' [已确认外部仓，不 halt]'
                issues.append(msg)
                if logger:
                    logger.info(f'[reconcile] {msg}')
                continue
            issues.append(msg)
            if logger:
                logger.warning(f'[reconcile] {msg}')
            halt = True

    for inst, book_vol in claimed.items():
        ctp_vol = ctp_long.get(inst, 0)
        spread_vol = spread_long.get(inst, 0)
        strangle_vol = max(0, ctp_vol - spread_vol)
        if book_vol > strangle_vol:
            msg = f'{inst}: CSV={book_vol} > strangle_CTP={strangle_vol} (CSV ahead)'
            issues.append(msg)
            if logger:
                logger.warning(f'[reconcile] {msg}')
            halt = True

    if halt:
        import time as _time

        runtime = getattr(conn, '_runtime_state', None) or {}
        until = float(runtime.get('_reconcile_grace_until') or 0.0)
        if _time.time() < until:
            if logger:
                logger.warning(
                    '[reconcile] 处于豁免窗口 (derive 后)，'
                    f'{len(issues)} 条 strangle 差异仅记录不 halt'
                )
            clear_reconcile_transient_streak(conn, STRANGLE_TRANSIENT_STREAK_KEY)
            return False, issues

    clear_reconcile_transient_streak(conn, STRANGLE_TRANSIENT_STREAK_KEY)
    return halt, issues
