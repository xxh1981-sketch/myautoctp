"""宽跨成交 → strangle_positions.csv（仅 OrderRef 属于宽跨号段）。"""

from __future__ import annotations

import json
import os
from datetime import date
from typing import Any, Dict, List, Optional, Set

from import_strangle_positions import (
    apply_fill_to_csv,
    load_positions_csv,
    positions_csv_path,
    sync_strangle_leg_claims,
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
    """CTP Direction/Offset → pairtrade 常量方向。"""
    from pairtrade.constants import DIRECTION_BUY, DIRECTION_SELL, OFFSET_CLOSE, OFFSET_OPEN

    d = str(direction or '').strip()
    o = str(offset or '').strip()
    if not o or o == '?':
        o = OFFSET_OPEN
    direction_out = DIRECTION_BUY if d in ('0', DIRECTION_BUY) else DIRECTION_SELL
    offset_out = OFFSET_OPEN if o in ('0', OFFSET_OPEN) else OFFSET_CLOSE
    return direction_out, offset_out


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
        claims = apply_fill_to_csv(
            config, instrument, direction, offset, volume, logger,
        )
        if ledger is not None:
            ledger.set_leg_claims(claims)

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
            f'[宽跨持仓] 成交入账 OrderRef={order_ref} {instrument} '
            f'{direction}/{offset} x{volume}'
        )
    return True


def wire_strangle_trade_runtime(conn, ledger) -> None:
    """注册宽跨成交入账回调（OnRtnTrade / 重连回放共用 ledger）。"""
    from strangle_fill_sync import handle_strangle_trade_rtn

    conn._runtime_state['_strangle_ledger'] = ledger

    def _handler(c, p_trade, logger):
        handle_strangle_trade_rtn(c, p_trade, logger, ledger)

    conn._runtime_state['_strangle_trade_handler'] = _handler


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
) -> int:
    """
    重连/对账前：从 CTP 成交查询中回放尚未入账的宽跨成交（严格按 OrderRef）。
    返回新入账笔数。
    """
    trades = _trades_from_query(conn)
    if trades is None:
        if logger:
            logger.debug('[宽跨持仓] 成交查询不可用或失败，跳过回放')
        return 0

    from auto_strategy_order_ref import is_strangle_order_ref

    journal_file = _journal_path(config)
    applied = _load_applied_keys(journal_file)
    new_count = 0
    for trade in trades:
        if not is_strangle_order_ref(trade.get('order_ref'), config):
            continue
        if _trade_dedupe_key(trade) in applied:
            continue
        if apply_strangle_trade_record(config, ledger, trade, logger, journal_file):
            applied.add(_trade_dedupe_key(trade))
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
    from import_strangle_positions import save_positions_csv

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
        direction, offset = _map_direction_offset(trade.get('direction'), trade.get('offset'))
        from import_strangle_positions import _fill_volume_delta
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
