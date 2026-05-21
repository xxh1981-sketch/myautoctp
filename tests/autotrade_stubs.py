"""Minimal autotrade module stubs for CI unit tests (no D:\\autotrade required)."""

from __future__ import annotations

import os
import re
import sys
import types
from typing import Iterable


def _extract_symbol_prefix(inst: str) -> str:
    m = re.match(r'^([A-Za-z]+)', str(inst or ''))
    return m.group(1).lower() if m else ''


def _months_match(inst: str, month: str, normalized_month: str) -> bool:
    s = str(inst or '')
    return str(month) in s or str(normalized_month) in s


def _extract_strike_from_instrument(inst: str, normalized_month: str, option_type: str = 'C'):
    u = str(inst or '').upper()
    if option_type == 'C':
        if re.search(r'C\d', u) and not re.search(r'P\d', u.split('C')[0]):
            return 2400
        if re.search(r'C\d', u) and 'P' not in u:
            return 2400
        return None
    return None


def _sum_positions_margin_for_limit(conn, pos, config):
    return (0, {})


def _contract_case_variants(inst: str):
    s = str(inst or '').strip()
    if not s:
        return []
    return list(dict.fromkeys([s, s.upper(), s.lower()]))


def _install_strategy_order_ref(mod):
    mod.DEFAULT_STRANGLE_ORDER_REF_MIN = 500000

    def get_strangle_order_ref_min(config):
        return int(config.get('strangle', {}).get('order_ref_min', 500000))

    def is_strangle_order_ref(order_ref, config):
        try:
            return int(order_ref) >= get_strangle_order_ref_min(config)
        except (TypeError, ValueError):
            return False

    def is_spread_order_ref(order_ref, config):
        try:
            ref = int(order_ref)
        except (TypeError, ValueError):
            return False
        return 0 < ref < get_strangle_order_ref_min(config)

    mod.get_strangle_order_ref_min = get_strangle_order_ref_min
    mod.is_strangle_order_ref = is_strangle_order_ref
    mod.is_spread_order_ref = is_spread_order_ref


_STUB_BUILDERS = {
    'auto_connection': lambda mod: (
        setattr(mod, 'extract_symbol_prefix', _extract_symbol_prefix),
        setattr(mod, 'months_match', _months_match),
    ),
    'auto_connection_utils': lambda mod: (
        setattr(mod, 'months_match', _months_match),
        setattr(mod, 'contract_case_variants', _contract_case_variants),
    ),
    'auto_position': lambda mod: setattr(
        mod, 'extract_strike_from_instrument', _extract_strike_from_instrument,
    ),
    'auto_risk': lambda mod: setattr(
        mod, 'sum_positions_margin_for_limit', _sum_positions_margin_for_limit,
    ),
    'auto_strategy_order_ref': _install_strategy_order_ref,
}


def ensure_autotrade_stubs(modules: Iterable[str]) -> None:
    """Register stub modules only when not already imported (real autotrade wins)."""
    root = os.environ.get('AUTOTRADE_ROOT', '').strip()
    if root and os.path.isdir(root):
        return
    for name in modules:
        if name in sys.modules:
            continue
        mod = types.ModuleType(name)
        builder = _STUB_BUILDERS.get(name)
        if builder:
            builder(mod)
        sys.modules[name] = mod
