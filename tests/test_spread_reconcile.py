"""spread_reconcile unit tests"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ctp_bootstrap  # noqa: F401

from spread_reconcile import (
    _signed_from_position_row,
    ctp_spread_signed_claims,
    reconcile_spread_positions,
)
from spread_ledger import SpreadLegStore


class FakeConn:
    def __init__(self):
        self._runtime_state = {}

    def _normalize_month(self, symbol, month):
        return month


class TestSpreadReconcile(unittest.TestCase):

    def test_signed_row_long_short(self):
        inst, v = _signed_from_position_row(
            {'instrument': 'SA609C2400', 'direction': '2', 'position': 2},
        )
        self.assertEqual(v, 2)
        inst, v = _signed_from_position_row(
            {'instrument': 'SA609C2500', 'direction': '3', 'position': 1},
        )
        self.assertEqual(v, -1)

    def test_ctp_spread_claims(self):
        conn = FakeConn()
        tradeinfo = [{'future': 'SA', 'month': '609'}]
        positions = [
            {'instrument': 'SA609C2400', 'direction': '2', 'position': 1},
            {'instrument': 'SA609C2500', 'direction': '3', 'position': 2},
            {'instrument': 'SA609P2400', 'direction': '2', 'position': 3},
        ]
        claims = ctp_spread_signed_claims(conn, tradeinfo, positions)
        self.assertEqual(claims['SA609C2400'], 1)
        self.assertEqual(claims['SA609C2500'], -2)
        self.assertNotIn('SA609P2400', claims)

    def test_reconcile_match_no_halt(self):
        conn = FakeConn()
        store = SpreadLegStore()
        store.set_leg_claims({'SA609C2400': 1, 'SA609C2500': -1})
        conn._runtime_state['_spread_leg_store'] = store
        conn.query_positions_sync = lambda timeout=10: [
            {'instrument': 'SA609C2400', 'direction': '2', 'position': 1},
            {'instrument': 'SA609C2500', 'direction': '3', 'position': 1},
        ]
        tradeinfo = [{'future': 'SA', 'month': '609'}]
        cfg = {'dual_strategy': {'auto_sync_spread_positions_csv': False}}
        halt, issues = reconcile_spread_positions(
            conn, tradeinfo, None, config=cfg,
        )
        self.assertFalse(halt)
        self.assertEqual(issues, [])

    def test_reconcile_mismatch_halts(self):
        conn = FakeConn()
        store = SpreadLegStore()
        store.set_leg_claims({'SA609C2400': 1})
        conn._runtime_state['_spread_leg_store'] = store
        conn.query_positions_sync = lambda timeout=10: [
            {'instrument': 'SA609C2400', 'direction': '2', 'position': 2},
        ]
        tradeinfo = [{'future': 'SA', 'month': '609'}]
        cfg = {'dual_strategy': {'auto_sync_spread_positions_csv': False}}
        halt, issues = reconcile_spread_positions(
            conn, tradeinfo, None, config=cfg,
        )
        self.assertTrue(halt)
        self.assertTrue(issues)


if __name__ == '__main__':
    unittest.main()
