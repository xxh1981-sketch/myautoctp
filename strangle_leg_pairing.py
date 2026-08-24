"""Infer strangle groups from leg_claims when ledger positions[] is incomplete.

Pairing rule (unknown pairing):
  - Sort calls and puts by strike descending.
  - Repeatedly pair the highest remaining call with the highest remaining put.
  - Leftover legs stay single (unmatched).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from spread_contract_utils import (
    extract_month_from_contract,
    extract_strike_from_instrument,
    months_match,
    symbol_prefix,
)
from spread_ledger import SpreadLegStore

_log = logging.getLogger(__name__)

_INSTALLED = False
_INSTALL_ERROR: str = ''

INFERRED_FLAG = 'inferred_from_claims'
INFERRED_SINGLE_KIND = 'inferred_single'
INFERRED_COMPLETE_KIND = 'inferred_complete'
INFERRED_ORPHAN_KINDS = frozenset({INFERRED_SINGLE_KIND, INFERRED_COMPLETE_KIND})


def is_close_unmatched_item(item: dict) -> bool:
    """True for queue rows that close-only rebalance must still process."""
    if not isinstance(item, dict):
        return False
    kind = item.get('kind')
    if kind == 'close_chp_pending':
        return True
    if kind == INFERRED_SINGLE_KIND and item.get('stage') == 'close':
        return True
    return False


def _norm_inst(instrument: str) -> str:
    return str(instrument or '').strip().upper()


def _canonical_instrument(conn, instrument: str) -> str:
    """Align ledger/CSV instrument id with the CTP wire id."""
    from ctp_instrument import canonical_ctp_instrument

    return canonical_ctp_instrument(conn, instrument)


def has_executor_open_phase2_pending(ledger, symbol: str, month: str) -> bool:
    """True when autostraggle ``awaiting_phase2`` already owns open-leg phase 2."""
    if ledger is None or not hasattr(ledger, 'list_unmatched_legs'):
        return False
    for item in ledger.list_unmatched_legs(symbol=symbol, month=month):
        if item.get('kind') == 'awaiting_phase2' and item.get('stage') == 'open':
            return True
    return False


def executor_close_pending_instruments(ledger, symbol: str, month: str) -> frozenset:
    """Instruments already queued on ``close_chp_pending`` for this symbol/month."""
    if ledger is None or not hasattr(ledger, 'list_unmatched_legs'):
        return frozenset()
    out = set()
    for item in ledger.list_unmatched_legs(symbol=symbol, month=month):
        if item.get('kind') != 'close_chp_pending':
            continue
        leg = item.get('leg') or {}
        inst = _norm_inst(leg.get('inst'))
        if inst:
            out.add(inst)
    return frozenset(out)


def _try_entry_for_complete(
    conn,
    item: dict,
    vix_engine,
    config: dict,
    logger,
) -> Tuple[bool, dict]:
    """Return (ok, check_entry result) when environment allows strangle entry."""
    try:
        from straggle_signals import check_entry  # type: ignore

        entry = check_entry(conn, item, vix_engine, config, logger)
        return bool(entry.get('ok')), entry
    except Exception:
        return False, {}


def _inferred_open_allowed(
    ledger,
    symbol: str,
    month: str,
    config: dict,
    vol_of_combo: int,
) -> Tuple[bool, str]:
    """Risk gates that ``check_entry`` does not cover for补腿买入.

    ``can_open_new_group`` cannot be reused here: it rejects on
    ``has_blocking_unmatched``, which is always true while we are rebuilding the
    orphan queue, and would silently turn every完整化补腿 into a平仓.
    """
    try:
        if ledger.is_open_halted():
            return False, ledger.get_open_halt_reason() or '账本禁止新开'
        if ledger.is_on_cooldown(symbol, month):
            return False, '平仓冷却期内'
        cap = int(vol_of_combo or 0)
        if cap > 0 and ledger.count_open_groups(symbol, month) >= cap:
            return False, f'已满 {cap} 组'
    except Exception as e:
        # fail-closed：异常时禁止补腿买入，避免绕过 halt/冷却/组数上限
        return False, f'风控检查异常: {e}'
    return True, ''


def single_leg_should_close(
    conn,
    row: dict,
    symbol: str,
    month: str,
    vix_engine,
    config: dict,
    vol_basis: float,
) -> Tuple[bool, str]:
    """Same exit semantics as ``check_exit``, applied to one orphan leg."""
    import math

    from straggle_signals import _resolve_sigma, premium_rate

    sym = str(symbol).lower()
    str_cfg = config.get('strangle', {})
    close_days = str_cfg.get(
        'close_days_to_expiry', config.get('close_days_to_expiry', 30),
    )
    dte = conn.get_days_to_expiry(sym, month) if hasattr(conn, 'get_days_to_expiry') else None
    if dte is not None and dte < close_days:
        return True, f'剩余 {dte} 天 < {close_days} 天时间平仓'

    future_price = float((getattr(conn, 'future_prices', None) or {}).get(sym) or 0.0)
    if future_price <= 0:
        return False, ''

    buffer_pct = float(str_cfg.get('breakout_buffer_pct', 0.01))
    strike = float(row['strike'])
    if row['side'] == 'call':
        upper = strike * (1 + buffer_pct)
        if future_price > upper:
            return True, (
                f'期货 {future_price:.2f} > Call {strike}×{1 + buffer_pct:.2f}={upper:.2f} 突破平仓'
            )
    else:
        lower = strike * (1 - buffer_pct)
        if future_price < lower:
            return True, (
                f'期货 {future_price:.2f} < Put {strike}×{1 - buffer_pct:.2f}={lower:.2f} 突破平仓'
            )

    # 波动率（平价溢价率）平仓：rate > pr_exit_coef * sqrt(t) * sigma
    if dte is not None:
        sigma, sigma_src = _resolve_sigma(config, sym, float(vol_basis), None)
        if sigma > 0:
            rate = premium_rate(conn, sym, month, future_price)
            if rate is not None:
                t = max(dte / 365.0, 1.0 / 365.0)
                exit_coef = float(str_cfg.get('pr_exit_coef', 0.40))
                threshold = exit_coef * math.sqrt(t) * sigma
                if rate > threshold:
                    return True, (
                        f'溢价率 {rate:.4f} > {threshold:.4f} 波动率平仓 '
                        f'(sigma={sigma:.3f}/{sigma_src}, t={t:.3f})'
                    )

    return False, ''


def _build_inferred_complete_item(
    row: dict,
    entry: dict,
    symbol: str,
    month: str,
    vol_basis: float,
    future_price: float,
    conn=None,
) -> Optional[dict]:
    """Queue BUY of the missing leg to complete one strangle group."""
    if row['side'] == 'call':
        buy_inst = entry.get('put_inst')
        leg_label = 'put'
        call_inst = row['inst']
        put_inst = buy_inst
        call_strike = float(row['strike'])
        put_strike = float(entry.get('put_strike') or 0)
    else:
        buy_inst = entry.get('call_inst')
        leg_label = 'call'
        put_inst = row['inst']
        call_inst = buy_inst
        put_strike = float(row['strike'])
        call_strike = float(entry.get('call_strike') or 0)
    if not buy_inst or not call_inst or not put_inst:
        return None
    vol = int(row['vol'])
    buy_inst = _canonical_instrument(conn, buy_inst)
    call_inst = _canonical_instrument(conn, call_inst)
    put_inst = _canonical_instrument(conn, put_inst)
    filled_inst = _canonical_instrument(conn, row['inst'])
    return {
        'kind': INFERRED_COMPLETE_KIND,
        'symbol': symbol,
        'month': month,
        'leg': {'inst': buy_inst, 'label': leg_label},
        'filled_instrument': filled_inst,
        'volume': vol,
        'target_groups': vol,
        'action': 'BUY',
        'stage': 'open',
        'call_inst': call_inst,
        'put_inst': put_inst,
        'call_strike': call_strike,
        'put_strike': put_strike,
        'vol_basis': float(vol_basis),
        'b_retry_count': 0,
        'base_future_price': future_price,
        INFERRED_FLAG: True,
    }


def _is_put_instrument(instrument: str) -> bool:
    inst = (instrument or '').strip().upper()
    if not inst:
        return False
    if SpreadLegStore._is_call_instrument(inst):
        return False
    return bool(re.search(r'[-]?P[-]?\d', inst))


def classify_option_leg(
    instrument: str,
    conn,
    symbol: str,
    month: str,
) -> Optional[dict]:
    """Return {inst, strike, side} or None if not this symbol/month."""
    inst = str(instrument or '').strip()
    if not inst:
        return None
    sym = symbol_prefix(inst)
    if sym != str(symbol or '').lower():
        return None
    try:
        norm_month = conn._normalize_month(sym, month)
    except Exception:
        norm_month = str(month)
    if not months_match(inst, month, norm_month):
        contract_month = extract_month_from_contract(inst)
        if not contract_month or not months_match(inst, month, contract_month):
            return None
        norm_month = contract_month
    if SpreadLegStore._is_call_instrument(inst):
        side = 'call'
        opt = 'C'
    elif _is_put_instrument(inst):
        side = 'put'
        opt = 'P'
    else:
        return None
    strike = extract_strike_from_instrument(inst, norm_month, option_type=opt)
    if strike is None:
        return None
    return {'inst': inst, 'strike': float(strike), 'side': side}


def claims_for_symbol_month(
    claims: Dict[str, int],
    conn,
    symbol: str,
    month: str,
) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for inst, vol in (claims or {}).items():
        try:
            v = int(vol)
        except (TypeError, ValueError):
            continue
        if v <= 0:
            continue
        meta = classify_option_leg(inst, conn, symbol, month)
        if meta is None:
            continue
        key = _norm_inst(meta.get('inst') or inst)
        out[key] = out.get(key, 0) + v
    return out


def subtract_ledger_coverage(
    remaining: Dict[str, int],
    ledger,
    symbol: str,
    month: str,
) -> Dict[str, int]:
    """Remove volumes already attributed to open/closing ledger groups."""
    out = dict(remaining)
    for pos in ledger.list_positions(symbol, month):
        if pos.get('status') not in ('open', 'closing'):
            continue
        for key, inst_key in (
            ('call_volume', 'call_instrument'),
            ('put_volume', 'put_instrument'),
        ):
            inst = _norm_inst(pos.get(inst_key) or '')
            if not inst:
                continue
            try:
                vol = int(pos.get(key) or pos.get('groups') or 0)
            except (TypeError, ValueError):
                vol = 0
            if vol <= 0:
                continue
            cur = int(out.get(inst, 0))
            out[inst] = max(0, cur - vol)
            if out[inst] == 0:
                out.pop(inst, None)
    return out


def pair_calls_puts_descending(
    call_legs: List[dict],
    put_legs: List[dict],
) -> Tuple[List[dict], List[dict]]:
    """
    Pair highest-strike call with highest-strike put until one side is exhausted.

    Returns (paired_groups, single_legs).
    """
    calls = [
        {'inst': c['inst'], 'strike': c['strike'], 'remaining': int(c['vol'])}
        for c in sorted(call_legs, key=lambda x: (-x['strike'], x['inst']))
    ]
    puts = [
        {'inst': p['inst'], 'strike': p['strike'], 'remaining': int(p['vol'])}
        for p in sorted(put_legs, key=lambda x: (-x['strike'], x['inst']))
    ]
    paired: List[dict] = []
    ci = 0
    pi = 0
    while ci < len(calls) and pi < len(puts):
        while ci < len(calls) and calls[ci]['remaining'] <= 0:
            ci += 1
        while pi < len(puts) and puts[pi]['remaining'] <= 0:
            pi += 1
        if ci >= len(calls) or pi >= len(puts):
            break
        groups = min(calls[ci]['remaining'], puts[pi]['remaining'])
        if groups <= 0:
            break
        paired.append({
            'call_instrument': calls[ci]['inst'],
            'put_instrument': puts[pi]['inst'],
            'call_strike': calls[ci]['strike'],
            'put_strike': puts[pi]['strike'],
            'groups': groups,
        })
        calls[ci]['remaining'] -= groups
        puts[pi]['remaining'] -= groups

    singles: List[dict] = []
    for row in calls:
        if row['remaining'] > 0:
            singles.append({
                'inst': row['inst'],
                'strike': row['strike'],
                'vol': row['remaining'],
                'side': 'call',
            })
    for row in puts:
        if row['remaining'] > 0:
            singles.append({
                'inst': row['inst'],
                'strike': row['strike'],
                'vol': row['remaining'],
                'side': 'put',
            })
    return paired, singles


def infer_groups_from_remaining_claims(
    remaining: Dict[str, int],
    conn,
    symbol: str,
    month: str,
) -> Tuple[List[dict], List[dict]]:
    call_legs: List[dict] = []
    put_legs: List[dict] = []
    for inst, vol in remaining.items():
        meta = classify_option_leg(inst, conn, symbol, month)
        if meta is None:
            continue
        row = {'inst': inst, 'strike': meta['strike'], 'vol': int(vol)}
        if meta['side'] == 'call':
            call_legs.append(row)
        else:
            put_legs.append(row)
    return pair_calls_puts_descending(call_legs, put_legs)


def _aggregate_pairs(pairs: List[dict]) -> List[dict]:
    merged: Dict[Tuple[str, str], dict] = {}
    for row in pairs:
        key = (row['call_instrument'], row['put_instrument'])
        if key not in merged:
            merged[key] = dict(row)
        else:
            merged[key]['groups'] = int(merged[key]['groups']) + int(row['groups'])
    return list(merged.values())


def _tag_position(ledger, position_id: str, flag: str = INFERRED_FLAG) -> None:
    with ledger._lock:
        for p in ledger._data['positions']:
            if p.get('id') == position_id:
                p[flag] = True
                break
        ledger._save()


def _remove_inferred_position(ledger, position_id: str) -> None:
    with ledger._lock:
        ledger._data['positions'] = [
            p for p in ledger._data['positions']
            if p.get('id') != position_id
        ]
        ledger._data['unmatched_legs'] = [
            u for u in ledger._data['unmatched_legs']
            if u.get('position_id') != position_id
        ]
        ledger._save()


def _position_key(pos: dict) -> Tuple[str, str]:
    return (
        _norm_inst(pos.get('call_instrument') or ''),
        _norm_inst(pos.get('put_instrument') or ''),
    )


def _symbol_month_has_ctp_long(conn, symbol: str, month: str) -> bool:
    """CTP 该品种月是否仍有多头期权。查询失败视为仍有仓（保守，避免误 prune）。"""
    try:
        from straggle_position_sync import query_ctp_long_volumes
    except Exception:
        return True
    sym = str(symbol or '').strip().lower()
    if not sym:
        return True
    try:
        vols, ok = query_ctp_long_volumes(conn, {sym})
    except Exception:
        return True
    if not ok:
        return True
    try:
        norm = conn._normalize_month(symbol, month)
    except Exception:
        norm = month
    for inst, vol in (vols or {}).items():
        try:
            if int(vol) < 1:
                continue
        except (TypeError, ValueError):
            continue
        if months_match(inst, month, norm):
            return True
    return False


def sync_inferred_strangle_positions(
    ledger,
    symbol: str,
    month: str,
    conn,
    vol_basis: float,
    logger=None,
    vix_engine=None,
    config: dict = None,
    tradeinfo_item: dict = None,
) -> dict:
    """
    Ensure surplus leg_claims (after ledger coverage) appear as inferred groups
    or single-leg unmatched rows. Idempotent per symbol/month.
    """
    log = logger or _log
    sym = str(symbol).lower()
    try:
        from ctp_instrument import rewrite_ledger_instrument_ids

        n = rewrite_ledger_instrument_ids(ledger, conn)
        if n and log:
            log.info(f'[{symbol}] 账本合约代码已按交易所规则归一 {n} 处')
    except Exception as e:
        if log:
            log.debug(f'[{symbol}] 账本合约代码归一跳过: {e}')
    claims = claims_for_symbol_month(ledger.list_leg_claims(), conn, sym, month)
    if not claims:
        # 无认领时才清推断仓；若 CTP 仍有多头则保留（防 CSV 误清空后空仓再开）
        if not _symbol_month_has_ctp_long(conn, sym, month):
            _prune_stale_inferred(ledger, sym, month, set(), [], log)
        elif log:
            log.warning(
                f'[{symbol}] CSV/认领为空但 CTP 仍有 {month} 多头，保留推断仓、跳过 prune',
            )
        return {'pairs': 0, 'singles': 0}

    remaining = subtract_ledger_coverage(claims, ledger, sym, month)
    if not remaining:
        # 认领已被 open/closing 完全覆盖。若覆盖来自推断仓本身，绝不能再 prune，
        # 否则会出现「奇数轮推断→已满 / 偶数轮删掉→又开仓」的隔轮重开。
        # 仅当存在非推断（执行器）持仓覆盖时，才清理多余推断仓。
        if _has_executor_managed_open_group(ledger, sym, month):
            _prune_stale_inferred(ledger, sym, month, set(), [], log)
        return {'pairs': 0, 'singles': 0}

    raw_pairs, singles = infer_groups_from_remaining_claims(
        remaining, conn, sym, month,
    )
    target_pairs = _aggregate_pairs(raw_pairs)
    target_keys = {_position_key(p) for p in target_pairs}

    existing_inferred = [
        p for p in ledger.list_positions(sym, month)
        if p.get(INFERRED_FLAG) and p.get('status') in ('open', 'closing')
    ]
    existing_by_key = {_position_key(p): p for p in existing_inferred}

    for pair in target_pairs:
        key = _position_key(pair)
        found = existing_by_key.get(key)
        if found:
            ledger.set_position_groups(found['id'], int(pair['groups']))
            continue
        pos = ledger.create_position(
            sym, month,
            _canonical_instrument(conn, pair['call_instrument']),
            _canonical_instrument(conn, pair['put_instrument']),
            float(pair['call_strike']), float(pair['put_strike']),
            float(vol_basis),
            groups=int(pair['groups']),
        )
        _tag_position(ledger, pos['id'])

    for pos in existing_inferred:
        if _position_key(pos) not in target_keys:
            _remove_inferred_position(ledger, pos['id'])

    _sync_inferred_singles(
        ledger, sym, month, singles, conn, vix_engine, config,
        float(vol_basis), log, tradeinfo_item=tradeinfo_item,
    )
    _prune_stale_inferred(ledger, sym, month, target_keys, singles, log)

    if target_pairs or singles:
        log.debug(
            f'[{symbol}] 认领配对: {len(target_pairs)} 组推断宽跨, '
            f'{len(singles)} 条单腿',
        )
    return {'pairs': len(target_pairs), 'singles': len(singles)}


def _has_executor_managed_open_group(ledger, symbol: str, month: str) -> bool:
    """True when autostraggle executor already owns a complete open/closing group."""
    for pos in ledger.list_positions(symbol, month):
        if pos.get('status') not in ('open', 'closing'):
            continue
        if pos.get(INFERRED_FLAG):
            continue
        if int(pos.get('groups') or 0) >= 1:
            return True
    return False


def _sync_inferred_singles(
    ledger,
    symbol: str,
    month: str,
    singles: List[dict],
    conn,
    vix_engine,
    config: dict,
    vol_basis: float,
    logger,
    tradeinfo_item: dict = None,
) -> None:
    """Orphan legs: exit → SELL; entry ok → BUY missing leg; else SELL."""
    config = config or {}
    sym = str(symbol).lower()
    if _has_executor_managed_open_group(ledger, symbol, month):
        if logger and singles:
            logger.warning(
                f'[{symbol}] executor 已有 open/closing 整组，'
                f'{len(singles)} 条推断单腿本轮不入队（疑似认领口径偏差，'
                f'请核对 CSV 与 CTP）',
            )
        singles = []
    future_price = float((getattr(conn, 'future_prices', None) or {}).get(sym) or 0.0)
    ti = tradeinfo_item or {
        'future': symbol, 'month': month, 'vol_basis': vol_basis,
        'vol_of_combo': 1, 'min_tick': 0.01,
    }
    skip_open_inferred = has_executor_open_phase2_pending(ledger, symbol, month)
    close_pending_insts = executor_close_pending_instruments(ledger, symbol, month)
    if skip_open_inferred and logger:
        logger.debug(
            f'[{symbol}] 认领推断跳过开仓类单腿：'
            f'awaiting_phase2 已由执行器接管',
        )

    desired: Dict[str, dict] = {}
    if skip_open_inferred:
        singles = []
    for row in singles:
        inst = _canonical_instrument(conn, row['inst'])
        if _norm_inst(inst) in close_pending_insts:
            if logger:
                logger.debug(
                    f'[{symbol}] 认领推断跳过单腿平仓 {inst}：'
                    f'close_chp_pending 已排队',
                )
            continue
        base = {
            'symbol': symbol,
            'month': month,
            'volume': int(row['vol']),
            INFERRED_FLAG: True,
            'b_retry_count': 0,
        }
        if future_price <= 0:
            should_close, reason = single_leg_should_close(
                conn, row, symbol, month, vix_engine, config, vol_basis,
            )
            desired[inst] = {
                **base,
                'kind': INFERRED_SINGLE_KIND,
                'leg': {'inst': inst, 'label': row['side']},
                'stage': 'close',
                'action': 'SELL',
            }
            if logger:
                if should_close:
                    logger.info(
                        f'[{symbol}] 单腿 {inst} x{row["vol"]} '
                        f'平仓信号(期货价无效): {reason}',
                    )
                else:
                    logger.info(
                        f'[{symbol}] 单腿 {inst} x{row["vol"]} '
                        f'期货价无效，保守加入平仓队列',
                    )
            continue

        should_close, reason = single_leg_should_close(
            conn, row, symbol, month, vix_engine, config, vol_basis,
        )
        if should_close:
            desired[inst] = {
                **base,
                'kind': INFERRED_SINGLE_KIND,
                'leg': {'inst': inst, 'label': row['side']},
                'stage': 'close',
                'action': 'SELL',
                'base_future_price': future_price,
            }
            if logger:
                logger.info(
                    f'[{symbol}] 单腿 {inst} x{row["vol"]} 平仓信号: {reason}',
                )
            continue

        open_ok, open_block = _inferred_open_allowed(
            ledger, symbol, month, config, int(ti.get('vol_of_combo') or 0),
        )
        entry_ok, entry = (False, {})
        if open_ok:
            entry_ok, entry = _try_entry_for_complete(
                conn, ti, vix_engine, config, logger,
            )
        elif logger:
            logger.info(
                f'[{symbol}] 单腿 {inst} 不补腿（{open_block}），转平仓队列',
            )
        if entry_ok:
            complete = _build_inferred_complete_item(
                row, entry, symbol, month, vol_basis, future_price, conn=conn,
            )
            if complete is not None:
                desired[inst] = complete
                if logger:
                    buy_leg = complete['leg']['inst']
                    logger.info(
                        f'[{symbol}] 单腿 {inst} x{row["vol"]} '
                        f'建仓环境满足，补腿买入 {buy_leg}',
                    )
                continue

        desired[inst] = {
            **base,
            'kind': INFERRED_SINGLE_KIND,
            'leg': {'inst': inst, 'label': row['side']},
            'stage': 'close',
            'action': 'SELL',
            'base_future_price': future_price,
        }
        if logger:
            logger.info(
                f'[{symbol}] 单腿 {inst} x{row["vol"]} '
                f'未满足补腿/持有条件，加入平仓队列',
            )

    with ledger._lock:
        kept = []
        for item in ledger._data['unmatched_legs']:
            if (
                item.get('kind') in INFERRED_ORPHAN_KINDS
                and (item.get('symbol') or '').lower() == sym
                and item.get('month') == month
            ):
                continue
            kept.append(item)
        for item in desired.values():
            kept.append(item)
        ledger._data['unmatched_legs'] = kept
        ledger._save()


def _prune_stale_inferred(
    ledger,
    symbol: str,
    month: str,
    target_keys: set,
    singles: List[dict],
    logger,
) -> None:
    if target_keys or singles:
        return
    for pos in list(ledger.list_positions(symbol, month)):
        if pos.get(INFERRED_FLAG) and pos.get('status') in ('open', 'closing'):
            _remove_inferred_position(ledger, pos['id'])
    with ledger._lock:
        ledger._data['unmatched_legs'] = [
            u for u in ledger._data['unmatched_legs']
            if not (
                u.get('kind') in INFERRED_ORPHAN_KINDS
                and (u.get('symbol') or '').lower() == symbol.lower()
                and u.get('month') == month
            )
        ]
        ledger._save()


def _prune_inferred_open_queues(ledger, symbol: str, month: str) -> int:
    """Remove claimed-layer open queues when executor owns ``awaiting_phase2``."""
    sym = str(symbol or '').lower()
    if not sym or ledger is None:
        return 0
    removed = 0
    lock = getattr(ledger, '_lock', None)
    ctx = lock if lock is not None else _null_context()
    with ctx:
        unmatched = ledger._data.get('unmatched_legs', [])
        kept = []
        for item in unmatched:
            if (
                item.get('kind') in INFERRED_ORPHAN_KINDS
                and item.get('stage') == 'open'
                and (item.get('symbol') or '').lower() == sym
                and item.get('month') == month
            ):
                removed += 1
                continue
            kept.append(item)
        if removed:
            ledger._data['unmatched_legs'] = kept
            if hasattr(ledger, '_save'):
                ledger._save()
    return removed


def _null_context():
    from contextlib import nullcontext
    return nullcontext()


def should_suppress_inferred_open_rebalance(ledger, item: dict) -> bool:
    """True when rebalance must not act on inferred open rows (executor owns phase 2)."""
    if item.get('kind') not in INFERRED_ORPHAN_KINDS:
        return False
    if item.get('stage') != 'open':
        return False
    return has_executor_open_phase2_pending(
        ledger, item.get('symbol', ''), item.get('month', ''),
    )


def get_install_error() -> str:
    """Last install failure reason (empty when install succeeded)."""
    return _INSTALL_ERROR


def install_strangle_leg_pairing_patch() -> bool:
    """Patch process_strangle_symbol to sync inferred groups before each scan.

    Returns ``True`` on success (or already installed). On failure returns
    ``False`` and records the reason in :func:`get_install_error`; the caller
    must surface this rather than silently skipping naked-leg pairing protection.
    """
    global _INSTALLED, _INSTALL_ERROR
    if _INSTALLED:
        return True

    try:
        import straggle_processor as sp
    except Exception as e:
        _INSTALL_ERROR = f'import straggle_processor 失败: {e}'
        return False

    orig = getattr(sp, 'process_strangle_symbol', None)
    if orig is None:
        _INSTALL_ERROR = (
            'straggle_processor 缺少 process_strangle_symbol'
            '（autostraggle 版本不兼容？）'
        )
        return False

    if getattr(orig, '_leg_pairing_patched', False):
        _INSTALLED = True
        _INSTALL_ERROR = ''
        return True

    def patched(
        conn, item, vix_engine, config, logger,
        ledger, executor, circuit_breaker=None,
        allow_quarantine_close_only=False,
    ):
        try:
            sync_inferred_strangle_positions(
                ledger,
                item['future'],
                item['month'],
                conn,
                float(item.get('vol_basis') or 0),
                logger,
                vix_engine=vix_engine,
                config=config,
                tradeinfo_item=item,
            )
        except Exception as e:
            if logger:
                logger.warning(
                    f"[{item.get('future')}] 认领配对推断失败: {e}",
                )
        return orig(
            conn, item, vix_engine, config, logger,
            ledger, executor,
            circuit_breaker=circuit_breaker,
            allow_quarantine_close_only=allow_quarantine_close_only,
        )

    try:
        patched._leg_pairing_patched = True
        sp.process_strangle_symbol = patched
    except Exception as e:
        _INSTALL_ERROR = f'patch process_strangle_symbol 失败: {e}'
        return False

    try:
        from straggle_ledger import StrangleLedger
        from straggle_execution import StrangleExecutor
    except Exception as e:
        _INSTALL_ERROR = f'import straggle ledger/executor 失败: {e}'
        return False

    orig_add = StrangleLedger.add_awaiting_phase2
    if not getattr(orig_add, '_leg_pairing_patched', False):

        def patched_add_awaiting_phase2(self, item):
            result = orig_add(self, item)
            _prune_inferred_open_queues(
                self, item.get('symbol', ''), item.get('month', ''),
            )
            return result

        patched_add_awaiting_phase2._leg_pairing_patched = True
        StrangleLedger.add_awaiting_phase2 = patched_add_awaiting_phase2

    orig_rebal = StrangleExecutor.run_rebalance
    if not getattr(orig_rebal, '_leg_pairing_patched', False):

        def patched_run_rebalance(self, tradeinfo_by_key):
            seen = set()
            for row in self.ledger.list_unmatched_legs():
                sym = (row.get('symbol') or '').lower()
                month = row.get('month', '')
                key = (sym, month)
                if key in seen or not sym:
                    continue
                seen.add(key)
                if has_executor_open_phase2_pending(self.ledger, sym, month):
                    _prune_inferred_open_queues(self.ledger, sym, month)
            return orig_rebal(self, tradeinfo_by_key)

        patched_run_rebalance._leg_pairing_patched = True
        StrangleExecutor.run_rebalance = patched_run_rebalance

    _INSTALLED = True
    _INSTALL_ERROR = ''
    return True
