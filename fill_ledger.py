"""All-fill ledger CSV (English headers, UTF-8)."""

from __future__ import annotations

import csv
import io
import os
import threading
from typing import Any, Dict, List, Optional

from atomic_io import atomic_write_text
from trade_journal import (
    append_journal,
    load_applied_keys,
    trade_dedupe_key,
)
from trade_journal_lock import journal_lock

FILL_LEDGER_COLUMNS = [
    'instrument_code',
    'fill_price',
    'bid_price',
    'ask_price',
    'slippage_vs_mid',
    'fill_volume',
    'fill_side',
    'strategy',
]

_FILL_SIDE_VALUES = frozenset({
    'buy_open', 'sell_open', 'buy_close', 'sell_close',
})

_write_lock = threading.Lock()


def _project_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def fill_ledger_csv_path(config: dict) -> str:
    dual = config.get('dual_strategy') or {}
    path = dual.get(
        'fill_ledger_csv',
        os.path.join('data', 'fill_ledger.csv'),
    )
    if not os.path.isabs(path):
        path = os.path.join(_project_dir(), path)
    return path


def fill_ledger_journal_path(config: dict) -> str:
    dual = config.get('dual_strategy') or {}
    path = dual.get(
        'fill_ledger_journal',
        os.path.join('data', 'fill_ledger_journal.jsonl'),
    )
    if not os.path.isabs(path):
        path = os.path.join(_project_dir(), path)
    return path



def resolve_fill_side(direction: str, offset: str) -> str:
    """Map CTP direction/offset to buy_open | sell_open | buy_close | sell_close."""
    d = str(direction or '').strip()
    o = str(offset or '').strip()
    if not o or o == '?':
        o = '0'
    if len(o) > 1:
        o = o[0]
    is_buy = d in ('0', 'buy', 'Buy', 'BUY')
    is_open = o in ('0', 'open', 'Open', 'OPEN')
    if is_buy and is_open:
        return 'buy_open'
    if is_buy and not is_open:
        return 'buy_close'
    if not is_buy and is_open:
        return 'sell_open'
    return 'sell_close'


def resolve_strategy(order_ref, config: dict) -> str:
    from auto_strategy_order_ref import is_spread_order_ref, is_strangle_order_ref

    if is_strangle_order_ref(order_ref, config):
        return 'strangle'
    if is_spread_order_ref(order_ref, config):
        return 'spread'
    return 'other'


def _lookup_quote(conn, instrument: str):
    from auto_connection_utils import contract_case_variants

    inst = (instrument or '').strip()
    if not inst:
        return None
    for store_name in ('quotes', 'option_quotes'):
        store = getattr(conn, store_name, None)
        if not store:
            continue
        for key in contract_case_variants(inst):
            quote = store.get(key)
            if quote is not None:
                return quote
    return None


def _quote_prices(conn, instrument: str) -> tuple:
    quote = _lookup_quote(conn, instrument)
    if quote is None:
        return '', ''
    bid = float(getattr(quote, 'bid', 0) or 0)
    ask = float(getattr(quote, 'ask', 0) or 0)
    bid_s = f'{bid:.4f}' if bid > 0 else ''
    ask_s = f'{ask:.4f}' if ask > 0 else ''
    return bid_s, ask_s


def slippage_vs_mid(fill_price: float, bid: float, ask: float, fill_side: str) -> str:
    """
    Adverse slippage vs mid: positive = worse fill.
    buy_* : fill - mid ; sell_* : mid - fill
    """
    if fill_price <= 0 or bid <= 0 or ask <= 0:
        return ''
    mid = (bid + ask) / 2.0
    if fill_side.startswith('buy'):
        slip = fill_price - mid
    else:
        slip = mid - fill_price
    return f'{slip:.4f}'


def _ensure_csv_header(csv_path: str) -> None:
    if os.path.isfile(csv_path) and os.path.getsize(csv_path) > 0:
        return
    buf = io.StringIO()
    csv.writer(buf).writerow(FILL_LEDGER_COLUMNS)
    atomic_write_text(csv_path, buf.getvalue())


def append_fill_row(csv_path: str, row: Dict[str, Any]) -> None:
    _ensure_csv_header(csv_path)
    values = [row.get(col, '') for col in FILL_LEDGER_COLUMNS]
    # csv.writer.writerow 会对 file 对象做多次 .write（每字段 + 分隔符）；
    # 进程在中途被杀可能产生半行（缺字段或缺末尾 \n）。先序列化到 StringIO，
    # 再用单次 f.write 落盘，使整行写入对应一次系统调用，半行风险降到磁盘
    # 块级原子性范围（小行通常 < 4KB，在常见文件系统上为原子追加）。
    buf = io.StringIO()
    csv.writer(buf).writerow(values)
    line = buf.getvalue()
    with _write_lock:
        with open(csv_path, 'a', encoding='utf-8', newline='') as f:
            f.write(line)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass


def build_fill_row(conn, trade: dict, config: dict) -> Optional[Dict[str, Any]]:
    instrument = (trade.get('instrument') or '').strip()
    volume = int(trade.get('volume') or 0)
    fill_price = float(trade.get('price') or 0)
    if not instrument or volume <= 0 or fill_price <= 0:
        return None

    fill_side = resolve_fill_side(trade.get('direction'), trade.get('offset'))
    if fill_side not in _FILL_SIDE_VALUES:
        return None

    bid_s, ask_s = _quote_prices(conn, instrument)
    bid = float(bid_s) if bid_s else 0.0
    ask = float(ask_s) if ask_s else 0.0
    slip_s = slippage_vs_mid(fill_price, bid, ask, fill_side)

    return {
        'instrument_code': instrument,
        'fill_price': f'{fill_price:.4f}',
        'bid_price': bid_s,
        'ask_price': ask_s,
        'slippage_vs_mid': slip_s,
        'fill_volume': volume,
        'fill_side': fill_side,
        'strategy': resolve_strategy(trade.get('order_ref'), config),
    }


def apply_fill_record(
    conn,
    config: dict,
    trade: dict,
    logger=None,
    journal_file: str = None,
) -> bool:
    """Append one fill to CSV (idempotent). Returns True if newly written.

    The quote lookup happens outside ``journal_lock`` to keep the lock window
    short; the dedupe set is re-checked inside the lock to remain race-safe
    against concurrent OnRtnTrade replay.
    """
    journal_file = journal_file or fill_ledger_journal_path(config)
    dedupe_key = trade_dedupe_key(trade)

    if dedupe_key in load_applied_keys(
        journal_file, config, include_pending=True,
    ):
        return False

    row = build_fill_row(conn, trade, config)
    if row is None:
        return False

    with journal_lock(journal_file):
        if dedupe_key in load_applied_keys(
            journal_file, config, include_pending=True,
        ):
            return False
        append_journal(journal_file, {
            'dedupe_key': dedupe_key,
            'trade_id': trade.get('trade_id', ''),
            'order_ref': trade.get('order_ref', 0),
            'instrument': row['instrument_code'],
            'fill_side': row['fill_side'],
            'strategy': row['strategy'],
            'trade_date': trade.get('trade_date', ''),
            'trade_time': trade.get('trade_time', ''),
            'journal_state': 'pending',
        }, config)
        csv_path = fill_ledger_csv_path(config)
        append_fill_row(csv_path, row)
        append_journal(journal_file, {
            'dedupe_key': dedupe_key,
            'trade_id': trade.get('trade_id', ''),
            'order_ref': trade.get('order_ref', 0),
            'instrument': row['instrument_code'],
            'fill_side': row['fill_side'],
            'strategy': row['strategy'],
            'trade_date': trade.get('trade_date', ''),
            'trade_time': trade.get('trade_time', ''),
            'journal_state': 'applied',
        }, config)
    if logger:
        logger.info(
            f'[FillLedger] {row["instrument_code"]} {row["fill_side"]} '
            f'x{row["fill_volume"]} @{row["fill_price"]} ({row["strategy"]})'
        )
    try:
        from trade_feishu_notify import notify_fill_trade_async, unified_fill_feishu
        if unified_fill_feishu(config):
            notify_fill_trade_async(conn, trade, row, config, logger)
    except Exception as e:
        if logger:
            logger.debug(f'[FillLedger] Feishu notify skipped: {e}')
    return True


def handle_fill_rtn(conn, p_trade, logger=None) -> None:
    from pairtrade.models import safe_decode

    try:
        order_ref = int(p_trade.OrderRef)
    except (ValueError, TypeError):
        order_ref = 0

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
    config = getattr(conn, 'config', None) or {}
    apply_fill_record(conn, config, trade, logger)


_WIRE_KIND_FILL_LEDGER = 'fill_ledger'


def wire_fill_ledger(conn) -> None:
    """Register fill-ledger handler via the shared (kind→handler) dispatch
    table. See :func:`strangle_fill_sync._install_wire_handler` for the
    idempotency contract."""
    from strangle_fill_sync import _install_wire_handler

    def _handler(c, p_trade, logger):
        handle_fill_rtn(c, p_trade, logger)

    _install_wire_handler(conn, _WIRE_KIND_FILL_LEDGER, _handler)


def _trades_from_query(conn) -> Optional[List[dict]]:
    if not hasattr(conn, 'query_trades_sync'):
        return None
    return conn.query_trades_sync(timeout=12, use_cache=False)


def sync_fill_ledger_from_trades(
    conn,
    config: dict,
    logger=None,
    trades: Optional[List[dict]] = None,
) -> int:
    """Replay today's CTP trades missing from the fill ledger.

    ``trades`` may be reused from an earlier query in the same round to avoid
    extra CTP RPC during reconcile.
    """
    if trades is None:
        trades = _trades_from_query(conn)
    if trades is None:
        if logger:
            logger.debug('[FillLedger] trade query unavailable, skip replay')
        return 0

    journal_file = fill_ledger_journal_path(config)
    applied = load_applied_keys(journal_file, config, include_pending=True)
    new_count = 0
    for trade in trades:
        key = trade_dedupe_key(trade)
        if key in applied:
            continue
        if apply_fill_record(conn, config, trade, logger, journal_file):
            applied.add(key)
            new_count += 1

    if new_count and logger:
        logger.info(f'[FillLedger] replayed {new_count} fills from CTP query')
    elif logger:
        logger.debug('[FillLedger] no missing fills to replay')
    return new_count
