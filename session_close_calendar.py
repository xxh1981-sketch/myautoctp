"""Trading-session calendar helpers for T-10 / T-1 close guards.

``is_trading_time_at`` mirrors ``auto_processor.is_trading_time`` (exchange
order acceptance; commodity 10:15-10:30 rest only — CFFEX continuous).

``seconds_to_segment_end`` / ``get_session_phase`` use *close-guard* segment
boundaries: by default the morning micro-break is **not** a segment end, so
T-10/T-1 only apply before 11:30 / 15:00 / night close — not before 10:15.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

_CFFEX_INDEX = frozenset({'if', 'ih', 'ic', 'im', 'io', 'ho', 'mo'})
_CFFEX_TREASURY = frozenset({'t', 'tf', 'ts', 'tl'})
_SHFE_LONG_NIGHT = frozenset({'au', 'ag', 'cu', 'al', 'zn', 'pb', 'ni', 'sn'})
_INE_LONG_NIGHT = frozenset({'sc', 'bc'})

_MORNING_BREAK_END_MIN = 10 * 60 + 30  # 10:30
_MORNING_BREAK_START_MIN = 10 * 60 + 15  # 10:15


def _extract_product(symbol: str) -> str:
    m = re.match(r'([a-zA-Z]+)', str(symbol or ''))
    return m.group(1).lower() if m else ''


def _fallback_exchange(symbol: str) -> str:
    """pairtrade.exchange 不可用时按内置品种集合判断（勿一律当 DCE：
    会给 CFFEX 金融品种套上商品 10:15-10:30 小休，io/tf 在 10:20 误判 off）。"""
    product = _extract_product(symbol)
    if product in _CFFEX_INDEX or product in _CFFEX_TREASURY:
        return 'CFFEX'
    if product in _SHFE_LONG_NIGHT:
        return 'SHFE'
    if product in _INE_LONG_NIGHT:
        return 'INE'
    return 'DCE'


def _exchange_for(symbol: str) -> str:
    try:
        from pairtrade.exchange import get_exchange_type
        return get_exchange_type(symbol)
    except Exception:
        return _fallback_exchange(symbol)


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


def _cffex_day_trading_segments(product: str) -> List[Tuple[int, int]]:
    """Exchange order-acceptance windows (CFFEX continuous through 10:15-10:30)."""
    if product in _CFFEX_TREASURY:
        return [(9 * 60 + 15, 11 * 60 + 30), (13 * 60, 15 * 60 + 15)]
    if product in _CFFEX_INDEX:
        return [(9 * 60 + 30, 11 * 60 + 30), (13 * 60, 15 * 60)]
    return [(9 * 60 + 30, 11 * 60 + 30), (13 * 60, 15 * 60)]


def _cffex_day_close_guard_segments(product: str) -> List[Tuple[int, int]]:
    """T-10/T-1 segment ends — morning micro-break is not a close-guard boundary."""
    if product in _CFFEX_TREASURY:
        return [(9 * 60 + 15, 11 * 60 + 30), (13 * 60, 15 * 60 + 15)]
    if product in _CFFEX_INDEX:
        return [(9 * 60 + 30, 11 * 60 + 30), (13 * 60, 15 * 60)]
    return [(9 * 60 + 30, 11 * 60 + 30), (13 * 60, 15 * 60)]


def _commodity_day_trading_segments() -> List[Tuple[int, int]]:
    return [
        (9 * 60, _MORNING_BREAK_START_MIN),
        (_MORNING_BREAK_END_MIN, 11 * 60 + 30),
        (13 * 60 + 30, 15 * 60),
    ]


def _commodity_day_close_guard_segments(include_morning_break: bool) -> List[Tuple[int, int]]:
    if include_morning_break:
        return _commodity_day_trading_segments()
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


def _in_segment(elapsed_minutes: float, start: int, end: int) -> bool:
    return start <= elapsed_minutes <= end


def _elapsed_minutes(now: datetime) -> float:
    return now.hour * 60 + now.minute + now.second / 60.0 + now.microsecond / 6e7


def _trading_segments_for_symbol(
    symbol: str,
    config: Optional[dict],
) -> List[Tuple[int, int, bool]]:
    """Segments when the exchange accepts orders."""
    product = _extract_product(symbol)
    exchange = _exchange_for(symbol)
    overrides = ((config or {}).get('session_close_guard') or {}).get('night_end_overrides') or {}

    if exchange == 'CFFEX':
        # CFFEX 无夜盘。
        return [(a, b, False) for a, b in _cffex_day_trading_segments(product)]

    day_segs = _commodity_day_trading_segments()
    night_end, cross = _night_end_minutes(product, exchange, overrides)
    night_start = 21 * 60
    if cross:
        return [(a, b, False) for a, b in day_segs] + [(night_start, night_end, True)]
    return [(a, b, False) for a, b in day_segs] + [(night_start, night_end, False)]


def _close_guard_segments_for_symbol(
    symbol: str,
    config: Optional[dict],
) -> List[Tuple[int, int, bool]]:
    """Segments whose *end* triggers T-10/T-1 (morning break excluded by default)."""
    product = _extract_product(symbol)
    exchange = _exchange_for(symbol)
    cfg = (config or {}).get('session_close_guard') or {}
    include_break = bool(cfg.get('include_morning_break', False))
    overrides = cfg.get('night_end_overrides') or {}

    if exchange == 'CFFEX':
        # CFFEX 无夜盘。
        return [(a, b, False) for a, b in _cffex_day_close_guard_segments(product)]

    day_segs = _commodity_day_close_guard_segments(include_break)
    night_end, cross = _night_end_minutes(product, exchange, overrides)
    night_start = 21 * 60
    if cross:
        return [(a, b, False) for a, b in day_segs] + [(night_start, night_end, True)]
    return [(a, b, False) for a, b in day_segs] + [(night_start, night_end, False)]


def is_trading_time_at(symbol: str, now: Optional[datetime] = None, config: Optional[dict] = None) -> bool:
    """Same semantics as ``auto_processor.is_trading_time`` but with explicit *now*."""
    now = now or datetime.now()
    current_minutes = now.hour * 60 + now.minute
    if not _weekday_allows_trading(now, current_minutes):
        return False

    elapsed = _elapsed_minutes(now)
    for start, end, cross in _trading_segments_for_symbol(symbol, config):
        if cross:
            if elapsed >= start or elapsed <= end:
                return True
        elif _in_segment(elapsed, start, end):
            return True
    return False


def seconds_to_segment_end(
    symbol: str,
    now: Optional[datetime] = None,
    config: Optional[dict] = None,
) -> Optional[float]:
    """Seconds until the close-guard segment ends; None if not in session."""
    now = now or datetime.now()
    if not is_trading_time_at(symbol, now, config):
        return None

    elapsed_in_min = _elapsed_minutes(now)

    best: Optional[float] = None
    for start, end, cross in _close_guard_segments_for_symbol(symbol, config):
        in_seg = False
        if cross:
            in_seg = elapsed_in_min >= start or elapsed_in_min <= end
        else:
            in_seg = start <= elapsed_in_min <= end
        if not in_seg:
            continue

        if cross:
            if elapsed_in_min >= start:
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
