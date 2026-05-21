"""merged_vix_cache 单元测试。"""

import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from merged_vix_cache import (
    SPREAD_ROUND_VIX_CACHE_KEY,
    begin_round_vix_cache,
    get_round_vix,
    wrap_vix_engine,
)


class TestMergedVixCache(unittest.TestCase):

    def _conn(self):
        conn = MagicMock()
        conn._runtime_state = {}
        return conn

    def test_begin_round_clears_spread_cache_only(self):
        conn = self._conn()
        conn._runtime_state[SPREAD_ROUND_VIX_CACHE_KEY] = {'sa': 12.3}
        conn._runtime_state['_round_vix_cache'] = {'sa': 99.9}

        begin_round_vix_cache(conn)

        self.assertEqual(conn._runtime_state[SPREAD_ROUND_VIX_CACHE_KEY], {})
        self.assertEqual(conn._runtime_state['_round_vix_cache'], {'sa': 99.9})

    def test_get_round_vix_caches_per_symbol(self):
        conn = self._conn()
        begin_round_vix_cache(conn)
        engine = MagicMock()
        engine.calculate_vix.side_effect = [11.0, 22.0]

        first = get_round_vix(engine, 'SA', conn)
        second = get_round_vix(engine, 'SA', conn)
        other = get_round_vix(engine, 'cu', conn)

        self.assertEqual(first, 11.0)
        self.assertEqual(second, 11.0)
        self.assertEqual(other, 22.0)
        self.assertEqual(engine.calculate_vix.call_count, 2)

    def test_get_round_vix_without_cache_calls_engine_each_time(self):
        conn = MagicMock()
        del conn._runtime_state
        engine = MagicMock()
        engine.calculate_vix.return_value = 5.5

        v1 = get_round_vix(engine, 'SA', conn)
        v2 = get_round_vix(engine, 'SA', conn)

        self.assertEqual(v1, 5.5)
        self.assertEqual(v2, 5.5)
        self.assertEqual(engine.calculate_vix.call_count, 2)

    def test_wrap_vix_engine_proxy_delegates_and_caches(self):
        conn = self._conn()
        begin_round_vix_cache(conn)
        inner = MagicMock()
        inner.calculate_vix.return_value = 7.7
        inner.some_attr = 'ok'
        logger = MagicMock()

        proxy = wrap_vix_engine(inner, conn, logger)
        self.assertEqual(proxy.some_attr, 'ok')
        self.assertEqual(proxy.calculate_vix('sa', conn), 7.7)
        self.assertEqual(proxy.calculate_vix('sa', conn), 7.7)
        inner.calculate_vix.assert_called_once()


if __name__ == '__main__':
    unittest.main()
