"""Trading-session calendar helpers for T-10 / T-1 close guards.

Mirrors ``auto_processor.is_trading_time`` segment boundaries (day / night /
CFFEX) with an injectable ``now`` for tests.  Do not use for order routing
outside session-close guard — keep ``auto_processor.is_trading_time`` as the
runtime trading gate elsewhere.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

_CFFEX_INDEX = frozenset({'if', 'ih', 'ic', 'im', 'io', 'ho', 'mo'})
_CFFEX_TREASURY = frozenset({'t', 'tf', 'ts', 'tl'})
_SHFE_LONG_NIGHT = frozenset({'au', 'ag', 'cu', 'al', 'zn', 'pb', 'ni', 'sn'})
_INE_LONG_NIGHT = frozenset({'sc', 'bc'})

# Commodity micro-break (optional segment end).
_MORNING_BREAK_END_MIN = 10 * 60 + 30  # 10:30


def _extract_product(symbol: str) -> str:
    m = re.match(r'([a-zA-Z]+)', str(symbol or ''))
    return m.group(1).lower() if m else ''


def _night_end_minutes(product: str, exchange: str, overrides: Dict[str, str]) -> Tuple[int, bool]:
    """Return (end_minutes, cross_day). cross_day=True => ends 02:30 next calendar day."""
    ov = (overrides or {}).get(product) or (overrides or {}).get(product.upper())
    if ov:
        parts = str(ov).strip().split(':')
        h, mi = int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
        end = h * 60 + mi
        return (end, end <= 2 * 60 + 30)

    if (exchange == 'SHFE' and product in _SHFE_LONG_NIGHT) or (
        exchange == 'INE' and product in _INE_LONG_NIGHT
    ):
        return (2 * 60 + 30, True)
    return (23 * 60, False)


def _cffex_day_segments(product: str) -> List[Tuple[int, int]]:
    if product in _CFFEX_TREASURY:
        return [(9 * 60 + 15, 11 * 60 + 30), (13 * 60, 15 * 60 + 15)]
    if product in _CFFEX_INDEX:
        return [(9 * 60 + 30, 11 * 60 + 30), (13 * 60, 15 * 60)]
    return [(9 * 60 + 30, 11 * 60 + 30), (13 * 60, 15 * 60)]


def _commodity_day_segments(include_morning_break: bool) -> List[Tuple[int, int]]:
    if include_morning_break:
        return [
            (9 * 60, 10 * 60 + 15),
            (_MORNING_BREAK_END_MIN, 11 * 60 + 30),
            (13 * 60 + 30, 15 * 60),
        ]
    return [(9 * 60, 11 * 60 + 30), (13 * 60 + 30, 15 * 60)]


def _weekday_allows_trading(now: datetime, current_minutes: int) -> bool:
    wd = now.weekday()
    if wd == 5 and current_minutes <= 2 * 60 + 30:
        return True
    if wd >= 5:
        return False
    if wd == 0 and current_minutes <= 2 * 60 + 30:
        return False
    return True


def _in_segment(current_minutes: int, start: int, end: int) -> bool:
    return start <= current_minutes <= end


def _segments_for_symbol(
    symbol: str,
    config: Optional[dict],
) -> List[Tuple[int, int, bool]]:
    """List of (start_min, end_min, cross_midnight) segments for *today*."""
    try:
        from pairtrade.exchange import get_exchange_type
    except Exception:
        get_exchange_type = lambda s: 'DCE'  # noqa: E731

    product = _extract_product(symbol)
    exchange = get_exchange_type(symbol)
    cfg = (config or {}).get('session_close_guard') or {}
    include_break = bool(cfg.get('include_morning_break', False))
    overrides = cfg.get('night_end_overrides') or {}

    if exchange == 'CFFEX':
        return [(a, b, False) for a, b in _cffex_day_segments(product)]

    day_segs = _commodity_day_segments(include_break)
    night_end, cross = _night_end_minutes(product, exchange, overrides)
    night_start = 21 * 60
    if cross:
        # Represent as (21:00, 02:30) with cross_midnight flag.
        return [(a, b, False) for a, b in day_segs] + [(night_start, night_end, True)]
    return [(a, b, False) for a, b in day_segs] + [(night_start, night_end, False)]


def is_trading_time_at(symbol: str, now: Optional[datetime] = None, config: Optional[dict] = None) -> bool:
    """Same semantics as ``auto_processor.is_trading_time`` but with explicit *now*."""
    now = now or datetime.now()
    current_minutes = now.hour * 60 + now.minute
    if not _weekday_allows_trading(now, current_minutes):
        return False

    for start, end, cross in _segments_for_symbol(symbol, config):
        if cross:
            if current_minutes >= start or current_minutes <= end:
                return True
        elif _in_segment(current_minutes, start, end):
            return True
    return False


def seconds_to_segment_end(
    symbol: str,
    now: Optional[datetime] = None,
    config: Optional[dict] = None,
) -> Optional[float]:
    """Seconds until the current session segment ends; None if not in session."""
    now = now or datetime.now()
    if not is_trading_time_at(symbol, now, config):
        return None

    current_minutes = now.hour * 60 + now.minute
    elapsed_in_min = current_minutes + now.second / 60.0 + now.microsecond / 6e7

    best: Optional[float] = None
    for start, end, cross in _segments_for_symbol(symbol, config):
        in_seg = False
        if cross:
            in_seg = elapsed_in_min >= start or elapsed_in_min <= end
        else:
            in_seg = start <= elapsed_in_min <= end
        if not in_seg:
            continue

        if cross:
            if elapsed_in_min >= start:
                # Same evening: until 24:00 + minutes to end.
                mins_left = (24 * 60 - elapsed_in_min) + end
            else:
                mins_left = end - elapsed_in_min
        else:
            mins_left = end - elapsed_in_min

        sec = max(0.0, mins_left * 60.0)
        if best is None or sec < best:
            best = sec
    return best


def segment_end_id(symbol: str, now: Optional[datetime] = None, config: Optional[dict] = None) -> str:
    """Stable id for deduping per-segment actions (e.g. T-1 cancel once)."""
    now = now or datetime.now()
    sec = seconds_to_segment_end(symbol, now, config)
    if sec is None:
        return ''
    end_at = now + timedelta(seconds=sec)
    return end_at.strftime('%Y%m%d-%H%M')


def get_session_phase(
    symbol: str,
    config: Optional[dict] = None,
    now: Optional[datetime] = None,
) -> str:
    """Return ``normal`` | ``t10`` | ``t1`` | ``off``."""
    cfg = (config or {}).get('session_close_guard') or {}
    if not cfg.get('enabled', True):
        return 'normal'

    now = now or datetime.now()
    if not is_trading_time_at(symbol, now, config):
        return 'off'

    sec = seconds_to_segment_end(symbol, now, config)
    if sec is None:
        return 'off'

    t1 = float(cfg.get('hard_stop_before_close_sec', 60))
    t10 = float(cfg.get('no_new_group_before_close_sec', 600))
    if sec <= t1:
        return 't1'
    if sec <= t10:
        return 't10'
    return 'normal'
