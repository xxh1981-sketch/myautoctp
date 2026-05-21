"""Spread fills -> spread_positions.csv (OrderRef in spread segment only)."""

from __future__ import annotations

import os
import re
from datetime import date, datetime
from typing import List, Optional

from import_spread_positions import (
    apply_fill_to_spread_csv,
    sync_spread_leg_claims,
)
from trade_journal import (
    append_journal,
    load_applied_keys,
    map_direction_offset,
    trade_dedupe_key,
)
from trade_journal_lock import journal_lock


_SYMBOL_PREFIX_RE = re.compile(r'^([A-Za-z]+)')


def _symbol_prefix(instrument: str) -> str:
    """从合约代码截取品种字母前缀（不区分大小写返回小写），
    如 ``SA609C1000`` → ``sa``，``RM509-C-9000`` → ``rm``。"""
    if not instrument:
        return ''
    m = _SYMBOL_PREFIX_RE.match(instrument.strip())
    return m.group(1).lower() if m else ''


def _config_symbols(config: dict, key: str) -> set:
    items = config.get(key) or []
    return {(it.get('future') or '').lower() for it in items if it.get('future')}


_UNEXPECTED_SPREAD_SYMBOL_WARNED: set = set()


def _journal_path(config: dict) -> str:
    dual = config.get('dual_strategy') or {}
    path = dual.get(
        'spread_trade_journal',
        os.path.join(os.path.dirname(__file__), 'data', 'spread_trade_journal.jsonl'),
    )
    if not os.path.isabs(path):
        path = os.path.join(os.path.dirname(__file__), path)
    return path


def apply_spread_trade_record(
    config: dict,
    store,
    trade: dict,
    logger=None,
    journal_file: str = None,
) -> bool:
    from auto_strategy_order_ref import is_spread_order_ref

    order_ref = trade.get('order_ref', 0)
    if not is_spread_order_ref(order_ref, config):
        return False

    journal_file = journal_file or _journal_path(config)
    dedupe_key = trade_dedupe_key(trade)

    with journal_lock(journal_file):
        applied = load_applied_keys(journal_file, config)
        if dedupe_key in applied:
            return False

        instrument = (trade.get('instrument') or '').strip()
        volume = int(trade.get('volume') or 0)
        if not instrument or volume <= 0:
            return False

        dual = config.get('dual_strategy') or {}
        require_match = dual.get('spread_fill_require_tradeinfo_match', True)
        spread_info = config.get('spread_tradeinfo') or []
        conn = config.get('_spread_fill_conn')
        if require_match and spread_info:
            from spread_claims_guard import instrument_in_spread_tradeinfo
            if not instrument_in_spread_tradeinfo(
                instrument, conn, spread_info,
            ):
                sym = _symbol_prefix(instrument)
                tag = f'skip_tradeinfo:{sym or instrument.lower()}'
                if tag not in _UNEXPECTED_SPREAD_SYMBOL_WARNED:
                    _UNEXPECTED_SPREAD_SYMBOL_WARNED.add(tag)
                    if logger:
                        logger.warning(
                            f'[价差持仓] 跳过入账 OrderRef={order_ref} {instrument} '
                            f'x{volume}：合约不在 spread tradeinfo（品种/月份不匹配）。'
                            '请核对 strategy_order_ref 是否把宽跨成交写进价差段。'
                        )
                append_journal(journal_file, {
                    'dedupe_key': dedupe_key,
                    'trade_id': trade.get('trade_id', ''),
                    'order_ref': order_ref,
                    'instrument': instrument,
                    'direction': trade.get('direction'),
                    'offset': trade.get('offset'),
                    'volume': volume,
                    'skipped': 'not_in_spread_tradeinfo',
                    'applied_on': date.today().isoformat(),
                }, config)
                return False

        # P4: spread OrderRef 命中但合约品种不在 spread_tradeinfo 中 → 异常
        # 配置 / 跨策略 OrderRef 串号；写入仍照旧（保持账本完整性），但每个
        # 品种打一次 warning，便于排查。
        sym = _symbol_prefix(instrument)
        spread_syms = _config_symbols(config, 'spread_tradeinfo')
        if sym and spread_syms and sym not in spread_syms:
            tag = f'spread:{sym}'
            if tag not in _UNEXPECTED_SPREAD_SYMBOL_WARNED:
                _UNEXPECTED_SPREAD_SYMBOL_WARNED.add(tag)
                if logger:
                    strangle_syms = _config_symbols(config, 'strangle_tradeinfo')
                    where = '宽跨配置' if sym in strangle_syms else '任何配置'
                    logger.warning(
                        f'[价差持仓] OrderRef={order_ref} 落在价差段，但合约 '
                        f'{instrument} 品种 {sym.upper()} 不在 spread_tradeinfo '
                        f'中（出现在 {where}）。可能是历史遗留或 OrderRef 段位'
                        '配置错乱，请核对 strategy_order_ref。'
                    )

        direction, offset = map_direction_offset(
            trade.get('direction'), trade.get('offset'),
        )
        claims = apply_fill_to_spread_csv(
            config, instrument, direction, offset, volume, logger,
        )
        if store is not None:
            store.set_leg_claims(claims)

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
            'applied_on': date.today().isoformat(),
        }, config)
    if logger:
        logger.info(
            f'[价差持仓] 成交入账 OrderRef={order_ref} {instrument} '
            f'{direction}/{offset} x{volume}'
        )
    return True


_WIRE_KIND_SPREAD = 'spread_fill_sync'


def wire_spread_trade_runtime(conn, store) -> None:
    cfg = getattr(conn, 'config', None)
    if isinstance(cfg, dict):
        cfg['_spread_fill_conn'] = conn
    """Register spread fill handler via the shared (kind→handler) dispatch
    table. See :func:`strangle_fill_sync._install_wire_handler` for the
    idempotency contract."""
    from strangle_fill_sync import _install_wire_handler

    conn._runtime_state['_spread_leg_store'] = store

    def _handler(c, p_trade, logger):
        handle_spread_trade_rtn(c, p_trade, logger, store)

    _install_wire_handler(conn, _WIRE_KIND_SPREAD, _handler)


def handle_spread_trade_rtn(conn, p_trade, logger, store=None) -> None:
    from pairtrade.models import safe_decode

    try:
        order_ref = int(p_trade.OrderRef)
    except (ValueError, TypeError):
        return

    if store is None:
        runtime = getattr(conn, '_runtime_state', None) or {}
        store = runtime.get('_spread_leg_store')

    config = getattr(conn, 'config', None) or {}
    if isinstance(config, dict):
        config['_spread_fill_conn'] = conn
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
    apply_spread_trade_record(config, store, trade, logger)


def _trades_from_query(conn) -> Optional[List[dict]]:
    if not hasattr(conn, 'query_trades_sync'):
        return None
    return conn.query_trades_sync(timeout=12, use_cache=False)


def sync_csv_from_spread_trades(
    conn,
    store,
    config: dict,
    logger=None,
    trades: Optional[List[dict]] = None,
) -> int:
    """Replay spread-segment trades not yet applied to CSV.

    ``trades`` may be supplied by callers that have already issued a
    ``query_trades_sync`` for the round, sparing the CTP another full query.
    """
    if trades is None:
        trades = _trades_from_query(conn)
    if trades is None:
        if logger:
            logger.debug('[价差持仓] 成交查询不可用或失败，跳过回放')
        return 0

    from auto_strategy_order_ref import is_spread_order_ref

    journal_file = _journal_path(config)
    applied = load_applied_keys(journal_file, config)
    new_count = 0
    for trade in trades:
        if not is_spread_order_ref(trade.get('order_ref'), config):
            continue
        key = trade_dedupe_key(trade)
        if key in applied:
            continue
        if apply_spread_trade_record(config, store, trade, logger, journal_file):
            applied.add(key)
            new_count += 1

    if new_count and logger:
        logger.info(f'[价差持仓] 自 CTP 成交回放 {new_count} 笔价差入账')
    elif logger:
        logger.debug('[价差持仓] 无待回放价差成交')

    sync_spread_leg_claims(store, config, logger=logger)
    return new_count


def count_spread_filled_open_orders(conn, config: dict, timeout: float = 2) -> Optional[int]:
    """
    Today's *fully filled* open orders restricted to the spread OrderRef segment.

    Filter rules mirror ``auto_query_service.get_filled_open_order_count`` exactly
    (``offset='0'``, ``status='0'``, ``insert_date == today``) and then add a
    spread-only OrderRef gate. Partial fills cancelled afterwards therefore are
    not counted; this matches the autotrade single-strategy semantics and is
    treated as a *coarse* throttle by design (see ``docs/GUIDE.md`` §10.4).

    Returns ``None`` when the order query is unavailable / fails, so callers can
    fall back to ``conn.get_filled_open_order_count``.
    """
    if not hasattr(conn, 'query_orders_sync'):
        return None
    try:
        orders = conn.query_orders_sync(timeout=timeout)
    except Exception:
        return None
    if orders is None:
        return None

    from auto_strategy_order_ref import is_spread_order_ref

    today_str = datetime.now().strftime('%Y%m%d')
    count = 0
    for order in orders:
        if order.get('offset') != '0':
            continue
        if order.get('status') != '0':
            continue
        if order.get('insert_date', '') != today_str:
            continue
        if not is_spread_order_ref(order.get('order_ref'), config):
            continue
        count += 1
    return count
