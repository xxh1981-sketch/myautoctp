"""价差认领与 CTP 物理持仓收敛（已平仓 / 拒平超持仓量场景）。"""

from __future__ import annotations

from typing import Dict, Set

from spread_contract_utils import (
    extract_month_from_contract,
    months_match,
    symbol_prefix as extract_symbol_prefix,
)
from spread_ledger import SpreadLegStore, store_from_conn


def spread_claims_for_symbol_month(
    store: SpreadLegStore,
    conn,
    symbol: str,
    month: str,
) -> Dict[str, int]:
    """Spread-owned call claims for one symbol/month (signed volume)."""
    sym = symbol.lower()
    normalized_month = conn._normalize_month(symbol, month)
    out: Dict[str, int] = {}
    for inst, vol in store.list_leg_claims().items():
        vol = int(vol)
        if vol == 0:
            continue
        if extract_symbol_prefix(inst) != sym:
            continue
        if not months_match(inst, month, normalized_month):
            continue
        if not SpreadLegStore._is_call_instrument(inst):
            continue
        out[str(inst).strip()] = vol
    return out


def _query_positions_for_residual(conn, *, use_cache: bool = True):
    """Query CTP positions; optionally bypass tracker cache when supported."""
    if use_cache:
        return conn.query_positions_sync(timeout=5)
    try:
        return conn.query_positions_sync(timeout=5, use_cache=False)
    except TypeError:
        return conn.query_positions_sync(timeout=5)


def spread_ctp_has_residual(
    conn, symbol: str, month: str, logger=None, *, use_cache: bool = True,
) -> bool:
    """
    True iff CTP still shows spread call legs for symbol+month.

    Strangle-owned long calls on the same symbol+month are subtracted first.
    Fail-closed: query error / None → True (treat as still holding).
    """
    try:
        positions = _query_positions_for_residual(conn, use_cache=use_cache)
    except Exception as e:
        if logger:
            logger.debug(f'[{symbol}] CTP 持仓复查失败: {e}，按仍有仓处理')
        return True
    if positions is None:
        # query_positions_sync 失败返回 None；不可与「查到空仓」混同，
        # 否则会误清认领导致漏平 / 重复开仓。
        if logger:
            logger.debug(f'[{symbol}] CTP 持仓复查不可用，按仍有仓处理')
        return True

    try:
        from spread_position_adjust import (
            _ledger_from_conn,
            exclude_strangle_from_positions,
            merge_strangle_owned_volumes,
        )

        vols = merge_strangle_owned_volumes(_ledger_from_conn(conn))
        if vols:
            positions = exclude_strangle_from_positions(positions, vols, None, symbol)
    except Exception:
        pass

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
        if not SpreadLegStore._is_call_instrument(inst):
            continue
        return True
    return False


def ctp_instrument_physical_volume(conn, instrument: str, logger=None) -> int:
    """Instrument CTP volume across directions; -1 if query failed."""
    inst = (instrument or '').strip().upper()
    if not inst:
        return 0
    try:
        positions = conn.query_positions_sync(timeout=5)
    except Exception as e:
        if logger:
            logger.debug(f'CTP 持仓查询失败 {inst}: {e}')
        return -1
    if positions is None:
        if logger:
            logger.debug(f'CTP 持仓查询不可用 {inst}')
        return -1
    total = 0
    for pos in positions:
        pos_inst = (
            pos.get('instrument') or pos.get('InstrumentID') or ''
        ).strip().upper()
        if pos_inst != inst:
            continue
        vol = int(pos.get('volume') or pos.get('Position') or pos.get('position') or 0)
        if vol > 0:
            total += vol
    return total


def _persist_spread_claims(
    store: SpreadLegStore,
    config: dict,
    claims: Dict[str, int],
    logger,
    symbol: str,
    reason: str,
) -> None:
    store.set_leg_claims(claims)
    from import_spread_positions import save_spread_positions_csv, spread_positions_csv_path

    path = spread_positions_csv_path(config)
    save_spread_positions_csv(path, claims)
    if logger:
        logger.info(f'[{symbol}] 价差认领已收敛 ({reason})')


def zero_symbol_month_spread_claims(
    store: SpreadLegStore,
    conn,
    symbol: str,
    month: str,
    config: dict,
    logger=None,
    reason: str = 'CTP已无价差仓',
) -> int:
    """Remove spread claims for symbol+month from store and CSV."""
    to_remove = spread_claims_for_symbol_month(store, conn, symbol, month)
    if not to_remove:
        return 0
    remove_keys = set(to_remove)
    new_claims = {
        inst: vol
        for inst, vol in store.list_leg_claims().items()
        if inst not in remove_keys
    }
    _persist_spread_claims(store, config, new_claims, logger, symbol, reason)
    return len(to_remove)


def converge_flat_spread_claims(
    conn,
    store: SpreadLegStore,
    symbol: str,
    month: str,
    config: dict,
    logger=None,
) -> int:
    """
    CTP 已无价差仓但 CSV/store 仍有认领 → 清零认领。

    两次 residual 检查（缓存 + 强制刷新）：防 tracker 假 flat 误清认领。

    Returns:
        清零的合约条数
    """
    if not spread_claims_for_symbol_month(store, conn, symbol, month):
        return 0
    if spread_ctp_has_residual(conn, symbol, month, logger, use_cache=True):
        return 0
    if spread_ctp_has_residual(conn, symbol, month, logger, use_cache=False):
        if logger:
            logger.info(
                f'[{symbol}] CTP 二次确认仍有价差仓，跳过认领收敛'
            )
        return 0
    return zero_symbol_month_spread_claims(
        store, conn, symbol, month, config, logger, 'CTP已无价差仓',
    )


def _months_for_symbol_claims(
    store: SpreadLegStore,
    conn,
    symbol: str,
    instrument: str = '',
) -> Set[str]:
    sym = symbol.lower()
    inst_u = (instrument or '').strip().upper()
    months: Set[str] = set()
    for claim_inst in store.list_leg_claims():
        if inst_u and claim_inst.strip().upper() != inst_u:
            continue
        if extract_symbol_prefix(claim_inst) != sym:
            continue
        if not SpreadLegStore._is_call_instrument(claim_inst):
            continue
        raw_month = extract_month_from_contract(claim_inst)
        if not raw_month:
            continue
        try:
            months.add(conn._normalize_month(symbol, raw_month))
        except Exception:
            months.add(raw_month)
    return months


def handle_benign_over_close_spread_reject(
    conn,
    store: SpreadLegStore,
    symbol: str,
    instrument: str,
    config: dict,
    logger=None,
) -> int:
    """
    错误码 30「平仓量超过持仓量」：对已 flat 的价差合约收敛认领。
    """
    inst = (instrument or '').strip().upper()
    sym = (symbol or '').strip().upper()
    n = 0

    if inst:
        ctp_vol = ctp_instrument_physical_volume(conn, inst, logger)
        if ctp_vol == 0:
            claims = dict(store.list_leg_claims())
            keys_to_remove = [k for k in claims if k.strip().upper() == inst]
            if keys_to_remove:
                for k in keys_to_remove:
                    claims.pop(k)
                _persist_spread_claims(
                    store, config, claims, logger, sym, f'拒平收敛 {inst}',
                )
                n += len(keys_to_remove)

    for month in _months_for_symbol_claims(store, conn, sym, inst):
        n += converge_flat_spread_claims(
            conn, store, sym, month, config, logger,
        )
    return n


def handle_benign_over_close_reject_dual(
    conn,
    ledger,
    symbol: str,
    instrument: str,
    config: dict,
    logger=None,
    strangle_logger=None,
) -> tuple:
    """宽跨 + 价差双侧已无仓拒平收敛。"""
    sl = strangle_logger or logger
    n_str = 0
    if ledger is not None:
        from straggle_position_sync import handle_benign_over_close_reject

        n_str = handle_benign_over_close_reject(
            conn, ledger, symbol, instrument, config, sl,
        )

    n_sp = 0
    store = store_from_conn(conn)
    if store is not None:
        n_sp = handle_benign_over_close_spread_reject(
            conn, store, symbol, instrument, config, logger,
        )
    return n_str, n_sp
