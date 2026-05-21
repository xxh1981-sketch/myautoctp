"""Exclude strangle-owned long options from spread A/B position analysis."""

from __future__ import annotations

from typing import Dict, List

_ORIG_ANALYZE = None
_ORIG_CHECK_LIMITS = None


def _rebind_auto_processor_attr(attr: str, value) -> None:
    import sys

    mod = sys.modules.get('auto_processor')
    if mod is not None and hasattr(mod, attr):
        setattr(mod, attr, value)


def merge_strangle_owned_volumes(ledger) -> Dict[str, int]:
    """Physical long-option volumes owned by strangle (CSV claims + unmatched legs)."""
    if ledger is None:
        return {}
    vols: Dict[str, int] = {}
    for inst, v in ledger.list_leg_claims().items():
        key = str(inst).strip().upper()
        if key and int(v) > 0:
            vols[key] = vols.get(key, 0) + int(v)
    for item in ledger.list_unmatched_legs():
        inst = (
            item.get('filled_instrument')
            or (item.get('leg') or {}).get('inst')
            or ''
        ).strip()
        if not inst:
            continue
        leg = item.get('leg') or {}
        v = int(item.get('volume') or leg.get('volume') or 1)
        key = inst.upper()
        vols[key] = vols.get(key, 0) + max(v, 0)
    return vols


def exclude_strangle_from_positions(
    positions: List[dict],
    strangle_vols: Dict[str, int],
    logger=None,
    symbol: str = None,
) -> List[dict]:
    """
    Subtract strangle-claimed long volume before spread A/B analysis.
    Short calls (spread B) are untouched.
    """
    if not strangle_vols:
        return positions

    remaining = dict(strangle_vols)
    out: List[dict] = []
    for pos in positions or []:
        row = dict(pos)
        inst = (row.get('instrument') or row.get('InstrumentID') or '').strip()
        direction = str(row.get('direction') or row.get('PosiDirection') or '')
        vol = int(row.get('position') or row.get('volume') or row.get('Position') or 0)
        if vol <= 0:
            continue
        if direction in ('2', 2, 'LONG'):
            key = inst.upper()
            claim = remaining.get(key, 0)
            if claim > 0:
                sub = min(vol, claim)
                vol -= sub
                remaining[key] = claim - sub
                if logger and sub > 0:
                    logger.debug(
                        f'[{symbol or "?"}] spread analysis excludes strangle '
                        f'{inst} x{sub}'
                    )
        if vol <= 0:
            continue
        row['position'] = vol
        if 'volume' in row:
            row['volume'] = vol
        out.append(row)
    return out


def _ledger_from_conn(conn):
    runtime = getattr(conn, '_runtime_state', None) or {}
    return runtime.get('_strangle_ledger')


def install_spread_excludes_strangle(config: dict) -> None:
    """
    Patch auto_position so spread A/B counts skip strangle leg_claims.
    Idempotent for process restarts in same interpreter.
    """
    global _ORIG_ANALYZE, _ORIG_CHECK_LIMITS
    dual = config.get('dual_strategy') or {}
    if not dual.get('exclude_strangle_from_spread_positions', True):
        return
    if _ORIG_ANALYZE is not None:
        return

    import auto_position

    _ORIG_ANALYZE = auto_position.analyze_position_imbalance
    _ORIG_CHECK_LIMITS = auto_position.check_position_limits

    def patched_analyze(conn, positions, symbol, month, vol_of_combo, config, future_price, logger):
        ledger = _ledger_from_conn(conn)
        vols = merge_strangle_owned_volumes(ledger)
        if vols:
            positions = exclude_strangle_from_positions(positions, vols, logger, symbol)
        return _ORIG_ANALYZE(
            conn, positions, symbol, month, vol_of_combo, config, future_price, logger,
        )

    def patched_check(conn, positions, symbol, month, vol_of_combo, config):
        ledger = _ledger_from_conn(conn)
        vols = merge_strangle_owned_volumes(ledger)
        if vols:
            positions = exclude_strangle_from_positions(positions, vols, None, symbol)
        return _ORIG_CHECK_LIMITS(conn, positions, symbol, month, vol_of_combo, config)

    auto_position.analyze_position_imbalance = patched_analyze
    auto_position.check_position_limits = patched_check
    _rebind_auto_processor_attr('analyze_position_imbalance', patched_analyze)
