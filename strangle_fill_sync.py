"""宽跨成交 → strangle_positions.csv（仅 OrderRef 属于宽跨号段）。"""

from __future__ import annotations

import os
from datetime import date
from typing import Dict, List, Optional

from import_strangle_positions import (
    apply_fill_to_csv,
    load_positions_csv,
    positions_csv_path,
    save_positions_csv,
    sync_strangle_leg_claims,
)
from trade_journal import (
    append_journal,
    load_applied_keys,
    map_direction_offset,
    trade_dedupe_key,
)
from trade_journal_lock import journal_lock


def _journal_path(config: dict) -> str:
    dual = config.get('dual_strategy') or {}
    path = dual.get(
        'strangle_trade_journal',
        os.path.join(os.path.dirname(__file__), 'data', 'strangle_trade_journal.jsonl'),
    )
    if not os.path.isabs(path):
        path = os.path.join(os.path.dirname(__file__), path)
    return path


def apply_strangle_trade_record(
    config: dict,
    ledger,
    trade: dict,
    logger=None,
    journal_file: str = None,
) -> bool:
    """单笔宽跨成交写入 CSV（幂等）。返回 True 表示新写入。"""
    from auto_strategy_order_ref import is_strangle_order_ref

    order_ref = trade.get('order_ref', 0)
    if not is_strangle_order_ref(order_ref, config):
        return False

    journal_file = journal_file or _journal_path(config)
    dedupe_key = trade_dedupe_key(trade)

    with journal_lock(journal_file):
        applied = load_applied_keys(journal_file, config, include_pending=True)
        if dedupe_key in applied:
            return False

        instrument = (trade.get('instrument') or '').strip()
        volume = int(trade.get('volume') or 0)
        if not instrument or volume <= 0:
            return False

        append_journal(journal_file, {
            'dedupe_key': dedupe_key,
            'trade_id': trade.get('trade_id', ''),
            'order_ref': order_ref,
            'instrument': instrument,
            'direction': trade.get('direction'),
            'offset': trade.get('offset'),
            'volume': volume,
            'journal_state': 'pending',
            'applied_on': date.today().isoformat(),
        }, config)

        direction, offset = map_direction_offset(
            trade.get('direction'), trade.get('offset'),
        )
        claims = apply_fill_to_csv(
            config, instrument, direction, offset, volume, logger,
        )
        if ledger is not None:
            ledger.set_leg_claims(claims)

        append_journal(journal_file, {
            'dedupe_key': dedupe_key,
            'trade_id': trade.get('trade_id', ''),
            'order_ref': order_ref,
            'instrument': instrument,
            'direction': direction,
            'offset': offset,
            'volume': volume,
            'price': trade.get('price'),
            'trade_date': trade.get('trade_date', ''),
            'trade_time': trade.get('trade_time', ''),
            'journal_state': 'applied',
            'applied_on': date.today().isoformat(),
        }, config)
    if logger:
        logger.info(
            f'[宽跨持仓] 成交入账 OrderRef={order_ref} {instrument} '
            f'{direction}/{offset} x{volume}'
        )
    return True


_WIRE_KIND_STRANGLE = 'strangle_fill_sync'

# dispatcher 调用顺序：strangle → spread → fill_ledger → 遗留 prev handler
# 历史调用序列：merged_main 依次调用 strangle → spread → fill_ledger，
# 我们这里只保证幂等：每个 kind 在 dispatch 表里只占一个槽位，
# 重复 wire 同 kind 会覆盖同槽位而不是叠加。
_LEGACY_PREV_KEY = '__legacy_prev__'
_WIRE_ORDER = (
    'strangle_fill_sync', 'spread_fill_sync', 'fill_ledger', _LEGACY_PREV_KEY,
)


def _install_wire_handler(conn, kind: str, handler_fn) -> None:
    """把 ``handler_fn`` 注册到 conn 的 dispatch 表 (kind→handler)，
    然后重建统一 dispatcher 写回 ``_unified_trade_handler`` /
    ``_strangle_trade_handler``。

    幂等：重复 install 同 kind 只会覆盖该 kind 的槽位，dispatcher 整体
    长度不变；其它 kind 的 handler 保持不变。

    向后兼容：首次 install 时若 ``_unified_trade_handler`` 已是其他模块
    设置的 raw handler（非本机制的 dispatcher），自动把它保留到
    ``__legacy_prev__`` 槽位，dispatcher 末尾仍会调用，从而维持原"链式"
    契约。
    """
    runtime = conn._runtime_state
    table = runtime.get('_wire_handler_table')
    if table is None:
        table = {}
        runtime['_wire_handler_table'] = table
        existing = (
            runtime.get('_unified_trade_handler')
            or runtime.get('_strangle_trade_handler')
        )
        if existing is not None and not getattr(existing, '__wire_dispatch__', False):
            table[_LEGACY_PREV_KEY] = existing
    table[kind] = handler_fn

    def _dispatch(c, p_trade, logger):
        # 按固定顺序依次调用，未注册的 kind 跳过；前序 handler 异常不阻塞后续。
        for k in _WIRE_ORDER:
            fn = table.get(k)
            if fn is None:
                continue
            try:
                fn(c, p_trade, logger)
            except Exception as e:
                if logger:
                    logger.debug(f'[wire:{k}] handler error: {e}')

    _dispatch.__wire_dispatch__ = True
    runtime['_unified_trade_handler'] = _dispatch
    runtime['_strangle_trade_handler'] = _dispatch


def wire_strangle_trade_runtime(conn, ledger) -> None:
    """
    注册宽跨成交入账回调（OnRtnTrade / 重连回放共用 ledger）。

    与 wire_spread_trade_runtime / wire_fill_ledger 对称：通过统一的
    ``_install_wire_handler`` 注册到 (kind→handler) dispatch 表。
    SPI 端只调用 ``_strangle_trade_handler``（见
    ``auto_trading_spi.OnRtnTrade``），dispatcher 会按固定顺序依次触发
    strangle / spread / fill_ledger，每个 kind 只被调用一次（幂等）。
    """
    conn._runtime_state['_strangle_ledger'] = ledger

    def _handler(c, p_trade, logger):
        handle_strangle_trade_rtn(c, p_trade, logger, ledger)

    _install_wire_handler(conn, _WIRE_KIND_STRANGLE, _handler)


def handle_strangle_trade_rtn(conn, p_trade, logger, ledger=None) -> None:
    """OnRtnTrade 回调：仅宽跨号段更新 CSV。"""
    from pairtrade.models import safe_decode

    try:
        order_ref = int(p_trade.OrderRef)
    except (ValueError, TypeError):
        return

    if ledger is None:
        runtime = getattr(conn, '_runtime_state', None) or {}
        ledger = runtime.get('_strangle_ledger')

    config = getattr(conn, 'config', None) or {}
    trade = {
        'order_ref': order_ref,
        'instrument': safe_decode(p_trade.InstrumentID),
        'direction': safe_decode(p_trade.Direction),
        'offset': safe_decode(getattr(p_trade, 'OffsetFlag', '0')),
        'volume': int(p_trade.Volume),
        'price': float(p_trade.Price),
        'trade_id': safe_decode(getattr(p_trade, 'TradeID', '') or '').strip(),
        'trade_date': safe_decode(getattr(p_trade, 'TradeDate', '') or ''),
        'trade_time': safe_decode(getattr(p_trade, 'TradeTime', '') or ''),
    }
    apply_strangle_trade_record(config, ledger, trade, logger)


def _trades_from_query(conn) -> Optional[List[dict]]:
    if not hasattr(conn, 'query_trades_sync'):
        return None
    return conn.query_trades_sync(timeout=12, use_cache=False)


def sync_csv_from_strangle_trades(
    conn,
    ledger,
    config: dict,
    logger=None,
    trades: Optional[List[dict]] = None,
) -> int:
    """
    重连/对账前：从 CTP 成交查询中回放尚未入账的宽跨成交（严格按 OrderRef）。
    返回新入账笔数。

    ``trades`` 可由调用方预先 ``query_trades_sync`` 后注入，避免一轮对账中
    spread/strangle/fill_ledger 三处各调一次 CTP 查询（每次 ~12s）。
    """
    if trades is None:
        trades = _trades_from_query(conn)
    if trades is None:
        if logger:
            logger.debug('[宽跨持仓] 成交查询不可用或失败，跳过回放')
        return 0

    from auto_strategy_order_ref import is_strangle_order_ref

    journal_file = _journal_path(config)
    applied = load_applied_keys(journal_file, config, include_pending=True)
    new_count = 0
    for trade in trades:
        if not is_strangle_order_ref(trade.get('order_ref'), config):
            continue
        key = trade_dedupe_key(trade)
        if key in applied:
            continue
        if apply_strangle_trade_record(config, ledger, trade, logger, journal_file):
            applied.add(key)
            new_count += 1

    if new_count and logger:
        logger.info(f'[宽跨持仓] 自 CTP 成交回放 {new_count} 笔宽跨入账')
    elif logger:
        logger.debug('[宽跨持仓] 无待回放宽跨成交')

    sync_strangle_leg_claims(ledger, config, logger=logger)
    return new_count


def rebuild_csv_from_strangle_trades(
    conn,
    ledger,
    config: dict,
    logger=None,
) -> Dict[str, int]:
    """
    自今日宽跨成交重建 CSV（仅 OrderRef 过滤；用于极端恢复）。
    会覆盖现有 CSV 中由宽跨成交可解释的部分。
    """
    from auto_strategy_order_ref import is_strangle_order_ref
    from import_strangle_positions import _fill_volume_delta

    trades = _trades_from_query(conn)
    if trades is None:
        return load_positions_csv(positions_csv_path(config)) if os.path.isfile(
            positions_csv_path(config)) else {}

    claims: Dict[str, int] = {}
    for trade in sorted(trades, key=lambda t: (t.get('trade_date', ''), t.get('trade_time', ''), t.get('order_ref', 0))):
        if not is_strangle_order_ref(trade.get('order_ref'), config):
            continue
        instrument = (trade.get('instrument') or '').strip()
        volume = int(trade.get('volume') or 0)
        if not instrument or volume <= 0:
            continue
        direction, offset = map_direction_offset(trade.get('direction'), trade.get('offset'))
        delta = _fill_volume_delta(direction, offset, volume)
        if delta == 0:
            continue
        new_vol = int(claims.get(instrument, 0)) + delta
        if new_vol <= 0:
            claims.pop(instrument, None)
        else:
            claims[instrument] = new_vol

    path = positions_csv_path(config)
    save_positions_csv(path, claims)
    if ledger is not None:
        ledger.set_leg_claims(claims)
    if logger:
        logger.info(f'[宽跨持仓] 自宽跨成交重建 CSV: {len(claims)} 个合约 ({path})')
    return claims
