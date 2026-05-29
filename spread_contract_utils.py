"""Pure contract parsing helpers for spread/strangle modules (no autotrade import)."""

from __future__ import annotations

import re
from typing import Optional

_SYMBOL_PREFIX_RE = re.compile(r'^[A-Za-z]+')
_MONTH_RE = re.compile(r'^[a-zA-Z]+(\d{3,4})')


def symbol_prefix(instrument: str) -> str:
    if not instrument:
        return ''
    m = _SYMBOL_PREFIX_RE.match(str(instrument).strip())
    return m.group(0).lower() if m else ''


def extract_month_from_contract(instrument: str) -> Optional[str]:
    m = _MONTH_RE.match(str(instrument or '').strip())
    return m.group(1) if m else None


def months_match(contract: str, target_month: str, normalized_month: str) -> bool:
    contract_month = extract_month_from_contract(contract)
    if not contract_month:
        return False
    return contract_month == target_month or contract_month == normalized_month


def extract_strike_from_instrument(
    instrument: str,
    month: str,
    option_type: str | None = None,
) -> Optional[float]:
    """Match autotrade auto_position.extract_strike_from_instrument semantics."""
    if not instrument or not month:
        return None
    if month not in instrument:
        return None
    prefix_match = _SYMBOL_PREFIX_RE.match(instrument)
    if not prefix_match:
        return None
    prefix = prefix_match.group()
    remainder = instrument[len(prefix):]
    remainder_no_month = re.sub(r'^' + re.escape(month), '', remainder)
    type_chars = option_type.upper() if option_type else 'CP'
    strike_pattern = r'([' + type_chars + r'])-?(\d+(\.\d+)?)'
    match = re.search(strike_pattern, remainder_no_month.upper())
    if match:
        try:
            return float(match.group(2))
        except ValueError:
            return None
    return None
