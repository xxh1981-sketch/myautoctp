"""spread_position_sync unit tests"""

import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ctp_bootstrap  # noqa: F401

from spread_ledger import SpreadLegStore
from spread_position_sync import (
    converge_flat_spread_claims,
    handle_benign_over_close_spread_reject,
    spread_claims_for_symbol_month,
    spread_ctp_has_residual,
)


class FakeConn:
    def __init__(self, positions=None):
        self._positions = positions or []
        self._runtime_state = {}

    def _normalize_month(self, symbol, month):
        return month

    def query_positions_sync(self, timeout=5):
        return list(self._positions)


class TestSpreadPositionSync(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.csv_path = os.path.join(self.tmp.name, 'spread_positions.csv')
        self.config = {
            'dual_strategy': {'spread_positions_csv': self.csv_path},
        }
        self.store = SpreadLegStore()
        self.store.set_leg_claims({'SA609C2400': 2, 'SA609C2500': -2})
        self.logger = MagicMock()

    def tearDown(self):
        self.tmp.cleanup()

    def test_spread_claims_for_symbol_month(self):
        conn = FakeConn()
        claims = spread_claims_for_symbol_month(self.store, conn, 'SA', '609')
        self.assertEqual(claims, {'SA609C2400': 2, 'SA609C2500': -2})

    def test_converge_clears_claims_when_ctp_flat(self):
        conn = FakeConn(positions=[])
        n = converge_flat_spread_claims(
            conn, self.store, 'SA', '609', self.config, self.logger,
        )
        self.assertEqual(n, 2)
        self.assertEqual(self.store.list_leg_claims(), {})
        self.assertTrue(os.path.isfile(self.csv_path))

    def test_converge_noop_when_ctp_has_residual(self):
        conn = FakeConn(positions=[
            {'instrument': 'SA609C2400', 'direction': '2', 'position': 1},
        ])
        n = converge_flat_spread_claims(
            conn, self.store, 'SA', '609', self.config, self.logger,
        )
        self.assertEqual(n, 0)
        self.assertEqual(len(self.store.list_leg_claims()), 2)

    def test_spread_ctp_has_residual(self):
        conn = FakeConn(positions=[
            {'instrument': 'SA609C2500', 'direction': '3', 'position': 1},
        ])
        self.assertTrue(spread_ctp_has_residual(conn, 'SA', '609', self.logger))
        conn_flat = FakeConn(positions=[])
        self.assertFalse(spread_ctp_has_residual(conn_flat, 'SA', '609', self.logger))

    def test_converge_noop_when_query_returns_none(self):
        """query_positions_sync 失败返回 None，不得当成 CTP 无仓清零认领。"""
        conn = FakeConn()
        conn.query_positions_sync = lambda timeout=5: None
        n = converge_flat_spread_claims(
            conn, self.store, 'SA', '609', self.config, self.logger,
        )
        self.assertEqual(n, 0)
        self.assertEqual(len(self.store.list_leg_claims()), 2)
        self.assertTrue(spread_ctp_has_residual(conn, 'SA', '609', self.logger))

    def test_converge_noop_when_second_query_has_residual(self):
        """缓存假 flat、强制刷新仍有仓 → 不得清认领。"""
        conn = FakeConn()
        calls = {'n': 0}

        def query(timeout=5, use_cache=True):
            calls['n'] += 1
            if use_cache:
                return []
            return [{'instrument': 'SA609C2400', 'direction': '2', 'position': 1}]

        conn.query_positions_sync = query
        n = converge_flat_spread_claims(
            conn, self.store, 'SA', '609', self.config, self.logger,
        )
        self.assertEqual(n, 0)
        self.assertEqual(len(self.store.list_leg_claims()), 2)
        self.assertGreaterEqual(calls['n'], 2)

    def test_handle_benign_over_close_keeps_claims_when_query_none(self):
        conn = FakeConn()
        conn.query_positions_sync = lambda timeout=5: None
        n = handle_benign_over_close_spread_reject(
            conn, self.store, 'SA', 'SA609C2400', self.config, self.logger,
        )
        self.assertEqual(n, 0)
        self.assertIn('SA609C2400', self.store.list_leg_claims())

    def test_handle_benign_over_close_zeros_flat_instrument(self):
        conn = FakeConn(positions=[])
        n = handle_benign_over_close_spread_reject(
            conn, self.store, 'SA', 'SA609C2400', self.config, self.logger,
        )
        self.assertGreaterEqual(n, 1)
        self.assertNotIn('SA609C2400', self.store.list_leg_claims())


if __name__ == '__main__':
    unittest.main()
