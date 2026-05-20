"""Spread fills -> spread_positions.csv (OrderRef in spread segment only)."""

from __future__ import annotations

import json
import os
from datetime import date, datetime
from typing import Dict, List, Optional, Set

from import_spread_positions import (
    apply_fill_to_spread_csv,
    load_spread_positions_csv,
    spread_positions_csv_path,
    sync_spread_leg_claims,
)
from trade_journal_lock import journal_lock


def _journal_path(config: dict) -> str:
    dual = config.get('dual_strategy') or {}
    path = dual.get(
        'spread_trade_journal',
        os.path.join(os.path.dirname(__file__), 'data', 'spread_trade_journal.jsonl'),
    )
    if not os.path.isabs(path):
        path = os.path.join(os.path.dirname(__file__), path)
    return path


def _load_applied_keys(journal_file: str) -> Set[str]:
    keys: Set[str] = set()
    if not os.path.isfile(journal_file):
        return keys
    with open(journal_file, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = row.get('dedupe_key') or row.get('trade_id')
            if key:
                keys.add(str(key))
    return keys


def _append_journal(journal_file: str, row: dict) -> None:
    os.makedirs(os.path.dirname(journal_file) or '.', exist_ok=True)
    with open(journal_file, 'a', encoding='utf-8') as f:
        f.write(json.dumps(row, ensure_ascii=False) + '\n')


def _trade_dedupe_key(trade: dict) -> str:
    trade_id = (trade.get('trade_id') or '').strip()
    if trade_id:
        inst = (trade.get('instrument') or '').upper()
        return f'{inst}:{trade_id}'
    return '|'.join([
        str(trade.get('order_ref', '')),
        (trade.get('instrument') or '').upper(),
        str(trade.get('direction', '')),
        str(trade.get('offset', '')),
        str(trade.get('volume', '')),
        str(trade.get('price', '')),
        str(trade.get('trade_date', '')),
        str(trade.get('trade_time', '')),
    ])


def _map_direction_offset(direction: str, offset: str) -> tuple:
    from pairtrade.constants import DIRECTION_BUY, DIRECTION_SELL, OFFSET_CLOSE, OFFSET_OPEN

    d = str(direction or '').strip()
    o = str(offset or '').strip()
    if not o or o == '?':
        o = OFFSET_OPEN
    direction_out = DIRECTION_BUY if d in ('0', DIRECTION_BUY) else DIRECTION_SELL
    offset_out = OFFSET_OPEN if o in ('0', OFFSET_OPEN) else OFFSET_CLOSE
    return direction_out, offset_out


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
    dedupe_key = _trade_dedupe_key(trade)

    with journal_lock(journal_file):
        applied = _load_applied_keys(journal_file)
        if dedupe_key in applied:
            return False

        instrument = (trade.get('instrument') or '').strip()
        volume = int(trade.get('volume') or 0)
        if not instrument or volume <= 0:
            return False

        direction, offset = _map_direction_offset(
            trade.get('direction'), trade.get('offset'),
        )
        claims = apply_fill_to_spread_csv(
            config, instrument, direction, offset, volume, logger,
        )
        if store is not None:
            store.set_leg_claims(claims)

        _append_journal(journal_file, {
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
        })
    if logger:
        logger.info(
            f'[价差持仓] 成交入账 OrderRef={order_ref} {instrument} '
            f'{direction}/{offset} x{volume}'
        )
    return True


def wire_spread_trade_runtime(conn, store) -> None:
    """Register spread fill handler (chains after any existing trade handler)."""
    conn._runtime_state['_spread_leg_store'] = store
    prev = conn._runtime_state.get('_strangle_trade_handler')

    def _handler(c, p_trade, logger):
        handle_spread_trade_rtn(c, p_trade, logger, store)
        if prev:
            prev(c, p_trade, logger)

    conn._runtime_state['_strangle_trade_handler'] = _handler


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
) -> int:
    """Replay spread-segment trades not yet applied to CSV."""
    trades = _trades_from_query(conn)
    if trades is None:
        if logger:
            logger.debug('[价差持仓] 成交查询不可用或失败，跳过回放')
        return 0

    from auto_strategy_order_ref import is_spread_order_ref

    journal_file = _journal_path(config)
    applied = _load_applied_keys(journal_file)
    new_count = 0
    for trade in trades:
        if not is_spread_order_ref(trade.get('order_ref'), config):
            continue
        if _trade_dedupe_key(trade) in applied:
            continue
        if apply_spread_trade_record(config, store, trade, logger, journal_file):
            applied.add(_trade_dedupe_key(trade))
            new_count += 1

    if new_count and logger:
        logger.info(f'[价差持仓] 自 CTP 成交回放 {new_count} 笔价差入账')
    elif logger:
        logger.debug('[价差持仓] 无待回放价差成交')

    sync_spread_leg_claims(store, config, logger=logger)
    return new_count


def count_spread_filled_open_orders(conn, config: dict, timeout: float = 2) -> Optional[int]:
    """Today's filled open orders in the spread OrderRef segment only."""
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
