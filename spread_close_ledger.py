"""Spread close path driven by spread_positions.csv / SpreadLegStore (not raw CTP)."""

from __future__ import annotations

import time
from typing import List, Tuple

from auto_connection import extract_symbol_prefix, months_match
from spread_ledger import store_from_conn

_ORIG_PROCESS_CLOSE = None


def _rebind_auto_processor_attr(attr: str, value) -> None:
    import sys

    mod = sys.modules.get('auto_processor')
    if mod is not None and hasattr(mod, attr):
        setattr(mod, attr, value)


def build_positions_from_spread_claims(
    store,
    conn,
    symbol: str,
    month: str,
) -> List[dict]:
    """Convert spread leg claims to autotrade position rows for one symbol/month."""
    sym = symbol.lower()
    normalized_month = conn._normalize_month(symbol, month)
    out: List[dict] = []
    for inst, vol in store.list_leg_claims().items():
        vol = int(vol)
        if vol == 0:
            continue
        if extract_symbol_prefix(inst) != sym:
            continue
        if not months_match(inst, month, normalized_month):
            continue
        if not store._is_call_instrument(inst):
            continue
        if vol > 0:
            out.append({'instrument': inst, 'direction': '2', 'position': vol})
        else:
            out.append({'instrument': inst, 'direction': '3', 'position': -vol})
    return out


def count_spread_ab_from_store(
    store,
    conn,
    symbol: str,
    month: str,
) -> Tuple[int, int]:
    """Sum spread-owned A (long) / B (short) volumes from leg claims."""
    positions = build_positions_from_spread_claims(store, conn, symbol, month)
    a_cur = sum(int(p['position']) for p in positions if p.get('direction') == '2')
    b_cur = sum(int(p['position']) for p in positions if p.get('direction') == '3')
    return a_cur, b_cur


def process_close_from_spread_ledger(
    conn,
    item: dict,
    vix: float,
    config: dict,
    logger,
    positions=None,
) -> bool:
    """
    Same orchestration as auto_closer.process_close, but:
      - symbol positions come from SpreadLegStore
      - post-close confirmation counts ledger A/B, not CTP
    """
    from auto_utils import resolve_min_tick
    from auto_closer_conditions import (
        CLOSE_URGENCY_NORMAL,
        check_close_conditions_with_urgency,
    )
    from auto_closer_plan import (
        calculate_close_plan_VIX_case,
        calculate_close_plan_future_price_case,
    )
    from auto_closer_executor import execute_close_orders_with_limit

    symbol = item['future']
    month = item['month']
    vol_basis = item['vol_basis']
    min_tick = resolve_min_tick(conn, symbol, month, item['min_tick'], logger)

    store = store_from_conn(conn)
    if store is None:
        logger.warning(f'[{symbol}] 价差账本无 store，跳过账本平仓')
        return False

    symbol_positions = build_positions_from_spread_claims(store, conn, symbol, month)
    if not symbol_positions:
        logger.info(f'[{symbol}] 价差账本无该品种持仓，跳过平仓检查')
        return False

    for pos in symbol_positions:
        logger.info(
            f'[{symbol}] 价差账本选中: {pos.get("instrument")}, '
            f'direction={pos.get("direction")}, position={pos.get("position")}'
        )

    future_price = conn.future_prices.get(symbol.lower(), 0.0)

    vix_exit_multiplier = config.get('VIX_EXIT_MULTIPLIER', 1.0)
    vix_exit_threshold = vol_basis * 100 * vix_exit_multiplier
    dte = conn.get_days_to_expiry(symbol, month) if hasattr(conn, 'get_days_to_expiry') else None
    close_days = config.get('close_days_to_expiry', 2)
    expiry_close = dte is not None and dte <= close_days
    if future_price <= 0 and vix >= vix_exit_threshold and not expiry_close:
        logger.warning(
            f'[{symbol}] 期货价格无效且VIX({vix:.2f})未触发平仓条件'
            f'(阈值{vix_exit_threshold:.2f})，跳过平仓检查'
        )
        return False

    urgency, reason = check_close_conditions_with_urgency(
        conn, vix, vol_basis, future_price, symbol_positions, symbol, month, config, logger,
    )
    if not urgency:
        return False

    logger.warning(f'[{symbol}] 触发平仓({urgency}): {reason} [价差账本]')

    if urgency == CLOSE_URGENCY_NORMAL:
        logger.info(
            f'[{symbol}] 平仓模式: VIX触发(常规) (VIX={vix:.2f} < {vix_exit_threshold:.2f})'
        )
        if future_price <= 0:
            logger.warning(
                f'[{symbol}] VIX平仓时期货价格无效({future_price})，spread检查将使用宽松阈值'
            )
        plan = calculate_close_plan_VIX_case(
            conn, symbol_positions, symbol, month, config, logger, min_tick, future_price,
        )
    else:
        logger.info(f'[{symbol}] 平仓模式: 期货价格触发(紧急) (future={future_price:.2f})')
        plan = calculate_close_plan_future_price_case(
            conn, symbol_positions, symbol, month, future_price, config, logger, min_tick,
        )

    if not plan:
        logger.info(f'[{symbol}] 平仓计划为空，无需执行')
        return False

    result = execute_close_orders_with_limit(
        conn, plan, symbol, month, min_tick, config, logger, urgency=urgency,
    )
    if isinstance(result, tuple):
        success, _total_actual_volume = result
    else:
        success = result

    if not success:
        logger.error(f'[{symbol}] 平仓执行失败（部分成交可能已发生），仍触发冷却期')
        return True

    max_confirm_attempts = config.get('close_confirm_attempts', 6)
    confirm_interval = config.get('close_confirm_interval', 2)
    for attempt in range(max_confirm_attempts):
        time.sleep(confirm_interval)
        try:
            a_cur, b_cur = count_spread_ab_from_store(store, conn, symbol, month)
            logger.info(
                f'[{symbol}] 价差账本平仓确认尝试{attempt + 1}: A={a_cur}, B={b_cur}'
            )
            if a_cur == 0 and b_cur == 0:
                logger.info(f'[{symbol}] 价差账本持仓已确认全部平仓')
                return True
            if a_cur == 0 and b_cur > 0:
                if _ctp_still_has_residual(conn, symbol, month, logger):
                    logger.warning(
                        f'[{symbol}] 价差账本 A 已平完但 B 仍有 {b_cur} 手残留，需人工检查'
                    )
                    _notify_spread_close_residual(conn, config, logger, symbol, a_cur, b_cur)
                    return False
                logger.info(
                    f'[{symbol}] 价差账本残留 B={b_cur} 但 CTP 已清零，'
                    f'视为 OnRtnTrade 回报延迟，继续等待'
                )
                continue
            if a_cur > 0 and b_cur == 0:
                if _ctp_still_has_residual(conn, symbol, month, logger):
                    logger.warning(
                        f'[{symbol}] 价差账本 A 仍有 {a_cur} 手但 B 已清零，需关注'
                    )
                    try:
                        from auto_feishu import safe_notify
                        safe_notify(
                            'send_feishu_message',
                            f'⚠️ [{symbol}] 价差账本平仓后 A 残留 {a_cur} 手, B=0, 需人工检查',
                            config=config,
                        )
                    except Exception:
                        pass
                    return False
                logger.info(
                    f'[{symbol}] 价差账本残留 A={a_cur} 但 CTP 已清零，'
                    f'视为 OnRtnTrade 回报延迟，继续等待'
                )
                continue
        except Exception as e:
            logger.warning(f'[{symbol}] 价差账本平仓确认异常: {e}')

    logger.warning(f'[{symbol}] 价差账本平仓后仍有认领余量，请人工检查')
    return False


def _ctp_still_has_residual(conn, symbol: str, month: str, logger) -> bool:
    """Cross-check CTP positions; True iff CTP still shows any spread leg."""
    try:
        positions = conn.query_positions_sync(timeout=5) or []
    except Exception as e:
        logger.debug(f'[{symbol}] CTP 持仓复查失败: {e}，按账本残留判断')
        return True

    sym = symbol.lower()
    try:
        normalized_month = conn._normalize_month(symbol, month)
    except Exception:
        normalized_month = month

    for pos in positions:
        inst = (pos.get('instrument') or pos.get('InstrumentID') or '').strip()
        if not inst:
            continue
        if extract_symbol_prefix(inst) != sym:
            continue
        if not months_match(inst, month, normalized_month):
            continue
        vol = int(pos.get('volume') or pos.get('Position') or pos.get('position') or 0)
        if vol <= 0:
            continue
        from spread_ledger import SpreadLegStore
        if not SpreadLegStore._is_call_instrument(inst):
            continue
        return True
    return False


def _notify_spread_close_residual(conn, config, logger, symbol, a_cur, b_cur) -> None:
    try:
        from auto_feishu import send_feishu_message
        send_feishu_message(
            f'⚠️ **价差平仓不完整（账本）**\n\n'
            f'**品种**: {symbol}\n'
            f'**A 认领**: {a_cur}\n'
            f'**B 认领**: {b_cur}\n'
            f'请人工检查 spread_positions.csv 与 CTP 持仓。',
            config=config,
        )
    except Exception as e:
        logger.debug(f'[{symbol}] 价差平仓残留飞书通知失败: {e}')


def install_spread_close_from_ledger(config: dict) -> None:
    """Patch auto_closer.process_close to use SpreadLegStore in dual-strategy mode."""
    global _ORIG_PROCESS_CLOSE
    from spread_dual_config import spread_execution_from_ledger

    dual = config.get('dual_strategy') or {}
    if not dual.get('use_spread_leg_claims', True):
        return
    if not spread_execution_from_ledger(config):
        return
    if _ORIG_PROCESS_CLOSE is not None:
        return

    import auto_closer

    _ORIG_PROCESS_CLOSE = auto_closer.process_close

    def patched_process_close(conn, item, vix, config, logger, positions=None):
        store = store_from_conn(conn)
        if store is not None:
            return process_close_from_spread_ledger(
                conn, item, vix, config, logger, positions=positions,
            )
        logger.debug(
            f'[{item.get("future", "?")}] SpreadLegStore 不可用，回退 CTP 平仓'
        )
        return _ORIG_PROCESS_CLOSE(
            conn, item, vix, config, logger, positions=positions,
        )

    auto_closer.process_close = patched_process_close
    _rebind_auto_processor_attr('process_close', patched_process_close)
