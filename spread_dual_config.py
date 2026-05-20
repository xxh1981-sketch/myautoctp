"""Dual-strategy spread config helpers (no autotrade imports)."""


def spread_execution_from_ledger(config: dict) -> bool:
    dual = config.get('dual_strategy') or {}
    if not dual.get('use_spread_leg_claims', True):
        return False
    if dual.get('spread_execution_from_ledger', True):
        return True
    return bool(dual.get('spread_close_from_ledger', True))
