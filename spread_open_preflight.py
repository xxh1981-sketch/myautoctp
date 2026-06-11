"""价差开仓前轻量预判：账本无仓且 A 类已满时跳过整段 process_symbol（不影响平仓路径）。"""

from __future__ import annotations

import re
import time
from typing import Tuple

from spread_close_ledger import build_positions_from_spread_claims, store_from_conn
from spread_position_adjust import (
    _ledger_from_conn,
    exclude_strangle_from_positions,
    merge_strangle_owned_volumes,
)


def _spread_has_ledger_claims(conn, symbol: str, month: str) -> bool:
    store = store_from_conn(conn)
    if store is None:
        return False
    return bool(build_positions_from_spread_claims(store, conn, symbol, month))


def _count_a_from_positions(
    conn,
    positions,
    symbol: str,
    month: str,
    config: dict,
) -> int:
    """统计 tracker 持仓中的 A 类（long call），并扣除宽跨认领多头。"""
    dual = config.get('dual_strategy') or {}
    if dual.get('exclude_strangle_from_spread_positions', True):
        ledger = _ledger_from_conn(conn)
        vols = merge_strangle_owned_volumes(ledger)
        if vols:
            positions = exclude_strangle_from_positions(
                positions, vols, None, symbol,
            )

    sym_lower = symbol.lower()
    normalized_month = conn._normalize_month(symbol, month)
    a_current = 0

    try:
        from auto_constants import POSITION_LONG
        from auto_utils import extract_strike_from_instrument, months_match
    except ImportError:
        return 0

    for pos in positions or []:
        instrument = pos.get('instrument', '')
        direction = pos.get('direction', '')
        pos_volume = int(pos.get('position', 0) or 0)
        if pos_volume <= 0:
            continue
        if not months_match(instrument, month, normalized_month):
            continue
        m = re.match(r'^[a-zA-Z]+', instrument)
        if not m or m.group().lower() != sym_lower:
            continue
        strike = extract_strike_from_instrument(
            instrument, normalized_month, option_type='C',
        )
        if strike is None:
            continue
        if direction == POSITION_LONG:
            a_current += pos_volume
    return a_current


def estimate_spread_a_headroom(
    conn,
    item: dict,
    config: dict,
) -> Tuple[int, int, int]:
    """返回 (A_current, A_limit, planned_A_for_one_combo)。"""
    symbol = item['future']
    month = item['month']
    vol = int(item.get('vol_of_combo', 1) or 1)
    a_limit = vol * int(config.get('A_POSITION_LIMIT_RATIO', 1) or 1)
    planned = vol * int(config.get('A_TARGET_VOLUME', 1) or 1)

    tracker = getattr(conn, 'position_tracker', None)
    if tracker is None:
        return 0, a_limit, planned
    try:
        normalized_month = conn._normalize_month(symbol, month)
        positions = tracker.get_positions_for_symbol(
            symbol, month, normalized_month,
        )
    except Exception:
        positions = []
    a_current = _count_a_from_positions(conn, positions, symbol, month, config)
    return a_current, a_limit, planned


def should_skip_spread_open_only_scan(
    conn,
    item: dict,
    config: dict,
    spread_open_ok: bool,
) -> bool:
    """
    账本无价差腿、且再开 1 组必触发 A 类超限时，跳过本轮 process_symbol。

    与 executor 风控口径一致（tracker + 扣宽跨多头）；有账本认领时仍走完整路径（含平仓）。
    """
    if not spread_open_ok:
        return False
    dual = config.get('dual_strategy') or {}
    if not dual.get('exclude_strangle_from_spread_positions', True):
        return False

    symbol = item['future']
    month = item['month']
    if _spread_has_ledger_claims(conn, symbol, month):
        return False

    a_current, a_limit, planned = estimate_spread_a_headroom(conn, item, config)
    return a_current + planned > a_limit


_PREFLIGHT_LOG_COOLDOWN = 300.0


def process_spread_symbol(
    conn,
    item: dict,
    vix_engine,
    config: dict,
    logger,
    remaining_limit=None,
    *,
    spread_open_ok: bool = True,
) -> bool:
    """merged_main_loop 入口：必要时跳过无仓且不可开仓的价差扫描。"""
    import auto_processor

    if should_skip_spread_open_only_scan(conn, item, config, spread_open_ok):
        sym = item['future'].lower()
        a_current, a_limit, planned = estimate_spread_a_headroom(conn, item, config)
        runtime = getattr(conn, '_runtime_state', None) or {}
        log_key = f'_spread_preflight_skip_log_{sym}'
        now = time.time()
        if now - float(runtime.get(log_key, 0) or 0) >= _PREFLIGHT_LOG_COOLDOWN:
            if runtime is not None:
                runtime[log_key] = now
            logger.info(
                f'[{sym}] 跳过本轮价差扫描(账本无仓、开仓必拒): '
                f'A类 当前={a_current} 计划+{planned} > 限额{a_limit} '
                f'(多为宽跨占用，下轮仍检查)'
            )
        return False
    return auto_processor.process_symbol(
        conn, item, vix_engine, config, logger, remaining_limit=remaining_limit,
    )
