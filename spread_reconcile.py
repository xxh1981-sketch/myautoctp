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
) -> Dict[str, int]:
    """Signed net spread call volume from CTP (long +, short -) on spread tradeinfo."""
    out: Dict[str, int] = {}
    keys = _spread_symbol_months(spread_tradeinfo)
    if not keys:
        return out

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
            out[inst] = out.get(inst, 0) + signed
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


def reconcile_spread_positions(
    conn,
    spread_tradeinfo: list,
    logger,
    config: dict = None,
) -> Tuple[bool, List[str]]:
    """
    Compare spread_positions.csv (SpreadLegStore) to CTP on spread symbols.

    Returns (halt_open, issues). Mismatch blocks spread open/rebalance (not close).
    """
    issues: List[str] = []
    config = config or {}
    dual = config.get('dual_strategy') or {}
    store = store_from_conn(conn)

    if dual.get('auto_sync_spread_positions_csv', True) and store is not None:
        try:
            from spread_fill_sync import sync_csv_from_spread_trades
            sync_csv_from_spread_trades(conn, store, config, logger)
        except Exception as e:
            issues.append(f'spread CSV sync failed: {e}')
            if logger:
                logger.warning(f'[spread-reconcile] CSV sync failed: {e}')

    try:
        positions = conn.query_positions_sync(timeout=10) or []
    except Exception as e:
        issues.append(f'position query failed: {e}')
        return True, issues

    if store is None:
        issues.append('SpreadLegStore unavailable')
        return True, issues

    book = ledger_spread_signed_claims(conn, store, spread_tradeinfo)
    ctp = ctp_spread_signed_claims(conn, spread_tradeinfo, positions)
    instruments = set(book) | set(ctp)

    halt = False
    for inst in sorted(instruments):
        book_vol = int(book.get(inst, 0))
        ctp_vol = int(ctp.get(inst, 0))
        if ctp_vol == book_vol:
            continue
        if abs(ctp_vol) > abs(book_vol) or (ctp_vol != 0 and book_vol == 0):
            msg = f'{inst}: CTP={ctp_vol} CSV={book_vol} (CTP ahead)'
            halt = True
        else:
            msg = f'{inst}: CSV={book_vol} CTP={ctp_vol} (CSV ahead)'
            halt = True
        issues.append(msg)
        if logger:
            logger.warning(f'[spread-reconcile] {msg}')

    return halt, issues
