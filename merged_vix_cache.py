"""Spread-only round VIX cache (does not affect strangle calculate_vix_for_month)."""

from typing import Any, Optional

# Must differ from autostraggle straggle_vix.ROUND_VIX_CACHE_KEY ('_round_vix_cache').
SPREAD_ROUND_VIX_CACHE_KEY = '_spread_round_vix_cache'


def begin_round_vix_cache(conn) -> None:
    """Clear spread-round cache only; wide跨仍按 tradeinfo month 独立计算 VIX。"""
    conn._runtime_state[SPREAD_ROUND_VIX_CACHE_KEY] = {}


def _get_spread_round_vix_cache(conn) -> Optional[dict]:
    if not hasattr(conn, '_runtime_state'):
        return None
    raw = conn._runtime_state.get(SPREAD_ROUND_VIX_CACHE_KEY)
    return raw if isinstance(raw, dict) else None


def get_round_vix(engine, sym: str, conn, logger=None) -> Optional[float]:
    """Per-symbol VIX for spread path within one main-loop round."""
    key = sym.lower()
    cache = _get_spread_round_vix_cache(conn)
    if cache is None:
        return engine.calculate_vix(key, conn, logger)
    if key not in cache:
        cache[key] = engine.calculate_vix(key, conn, logger)
    return cache[key]


def wrap_vix_engine(engine, conn, logger):
    """Wrap VIXEngine.calculate_vix for spread; strangle keeps raw engine."""

    class _RoundVixEngineProxy:
        __slots__ = ('_engine', '_conn', '_logger')

        def __init__(self, inner, connection, log):
            self._engine = inner
            self._conn = connection
            self._logger = log

        def calculate_vix(self, sym, conn, logger=None):
            return get_round_vix(
                self._engine, sym, conn, logger or self._logger,
            )

        def __getattr__(self, name: str) -> Any:
            return getattr(self._engine, name)

    return _RoundVixEngineProxy(engine, conn, logger)
