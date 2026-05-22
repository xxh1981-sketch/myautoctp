"""Per-round regime VIX cache (spread + strangle share OI 近月算法)."""

from typing import Any, Optional

# 与 straggle_vix.MERGED_REGIME_VIX_CACHE_KEY 一致；宽跨独立运行可用 _round_vix_cache
SPREAD_ROUND_VIX_CACHE_KEY = '_spread_round_vix_cache'
REGIME_ROUND_VIX_CACHE_KEY = SPREAD_ROUND_VIX_CACHE_KEY


def begin_round_vix_cache(conn) -> None:
    """每轮主循环清空；价差/宽跨共用同一品种 VIX（engine.calculate_vix）。"""
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
    """Wrap VIXEngine.calculate_vix with per-round cache (spread + strangle)."""

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
