"""strangle_reconcile_dual unit tests"""

import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ctp_bootstrap  # noqa: F401

from spread_ledger import SpreadLegStore
from strangle_reconcile_dual import (
    collect_spread_long_call_volumes,
    reconcile_strangle_positions_dual,
    resolve_spread_long_call_volumes,
)


def _conn():
    conn = MagicMock()

    def _normalize(sym, month):
        if sym.lower() in ('sa', 'cf', 'ma') and month == '2609':
            return '609'
        return month

    conn._normalize_month = _normalize
    conn._runtime_state = {}
    return conn


class TestCollectSpreadLongCalls(unittest.TestCase):

    def test_counts_spread_a_legs_only(self):
        spread_info = [{'future': 'ma', 'month': '2609', 'vol_of_combo': 3}]
        positions = [
            {'instrument': 'MA609C2900', 'direction': '2', 'position': 1},
            {'instrument': 'MA609C2950', 'direction': '2', 'position': 5},
            {'instrument': 'MA609C2800', 'direction': '3', 'position': 10},
            {'instrument': 'MA609P2700', 'direction': '2', 'position': 2},
        ]
        out = collect_spread_long_call_volumes(_conn(), spread_info, positions)
        self.assertEqual(out.get('MA609C2900'), 1)
        self.assertEqual(out.get('MA609C2950'), 5)
        self.assertNotIn('MA609C2800', out)
        self.assertNotIn('MA609P2700', out)


class TestResolveSpreadClaims(unittest.TestCase):

    def test_uses_ledger_not_all_long_calls(self):
        conn = _conn()
        store = SpreadLegStore()
        store.set_leg_claims({'MA609C2900': 1, 'MA609C2950': -2})
        conn._runtime_state['_spread_leg_store'] = store
        positions = [
            {'instrument': 'MA609C2900', 'direction': '2', 'position': 2},
            {'instrument': 'MA609C2950', 'direction': '2', 'position': 5},
        ]
        cfg = {'dual_strategy': {'use_spread_leg_claims': True}}
        out = resolve_spread_long_call_volumes(conn, [], positions, cfg)
        self.assertEqual(out, {'MA609C2900': 1})
        self.assertNotIn('MA609C2950', out)


class TestReconcileDual(unittest.TestCase):

    def test_spread_only_no_halt_when_csv_empty(self):
        conn = _conn()
        store = SpreadLegStore()
        store.set_leg_claims({'MA609C2900': 1, 'MA609C2950': -2})
        conn._runtime_state['_spread_leg_store'] = store
        conn.query_positions_sync.return_value = [
            {'instrument': 'MA609C2900', 'direction': '2', 'position': 1},
            {'instrument': 'MA609C2950', 'direction': '3', 'position': 2},
        ]
        ledger = MagicMock()
        ledger.list_leg_claims.return_value = {}
        spread_info = [
            {'future': 'ma', 'month': '2609', 'vol_of_combo': 3},
        ]
        cfg = {
            'strangle': {'auto_sync_positions_csv': False},
            'dual_strategy': {
                'exclude_spread_from_strangle_reconcile': True,
                'auto_sync_spread_positions_csv': False,
                'use_spread_leg_claims': True,
            },
        }
        with unittest.mock.patch(
            'strangle_fill_sync.sync_csv_from_strangle_trades',
        ):
            halt, issues = reconcile_strangle_positions_dual(
                conn, ledger, {'ma'}, spread_info, None, config=cfg,
            )
        self.assertFalse(halt)
        self.assertEqual(issues, [])

    def test_dual_call_same_instrument_reconcile(self):
        conn = _conn()
        store = SpreadLegStore()
        store.set_leg_claims({'MA609C2900': 1})
        conn._runtime_state['_spread_leg_store'] = store
        conn.query_positions_sync.return_value = [
            {'instrument': 'MA609C2900', 'direction': '2', 'position': 2},
        ]
        ledger = MagicMock()
        ledger.list_leg_claims.return_value = {'MA609C2900': 1}
        cfg = {
            'strangle': {'auto_sync_positions_csv': False},
            'dual_strategy': {
                'exclude_spread_from_strangle_reconcile': True,
                'auto_sync_spread_positions_csv': False,
                'use_spread_leg_claims': True,
            },
        }
        with unittest.mock.patch(
            'strangle_fill_sync.sync_csv_from_strangle_trades',
        ):
            halt, issues = reconcile_strangle_positions_dual(
                conn, ledger, {'ma'}, [], None, config=cfg,
            )
        self.assertFalse(halt)
        self.assertEqual(issues, [])

    def test_strangle_orphan_still_halts(self):
        conn = _conn()
        store = SpreadLegStore()
        conn._runtime_state['_spread_leg_store'] = store
        conn.query_positions_sync.return_value = [
            {'instrument': 'SA609C1000', 'direction': '2', 'position': 2},
        ]
        ledger = MagicMock()
        ledger.list_leg_claims.return_value = {}
        cfg = {
            'strangle': {'auto_sync_positions_csv': False},
            'dual_strategy': {
                'exclude_spread_from_strangle_reconcile': True,
                'auto_sync_spread_positions_csv': False,
                'use_spread_leg_claims': True,
            },
        }
        with unittest.mock.patch(
            'strangle_fill_sync.sync_csv_from_strangle_trades',
        ):
            halt, issues = reconcile_strangle_positions_dual(
                conn, ledger, {'sa'}, [], None, config=cfg,
            )
        self.assertTrue(halt)
        self.assertEqual(len(issues), 1)

    def test_csv_ahead_halts(self):
        conn = _conn()
        store = SpreadLegStore()
        conn._runtime_state['_spread_leg_store'] = store
        conn.query_positions_sync.return_value = [
            {'instrument': 'SA609C1000', 'direction': '2', 'position': 1},
        ]
        ledger = MagicMock()
        ledger.list_leg_claims.return_value = {'SA609C1000': 2}
        cfg = {
            'strangle': {'auto_sync_positions_csv': False},
            'dual_strategy': {
                'exclude_spread_from_strangle_reconcile': True,
                'auto_sync_spread_positions_csv': False,
                'use_spread_leg_claims': True,
            },
        }
        with unittest.mock.patch(
            'strangle_fill_sync.sync_csv_from_strangle_trades',
        ):
            halt, issues = reconcile_strangle_positions_dual(
                conn, ledger, {'sa'}, [], None, config=cfg,
            )
        self.assertTrue(halt)
        self.assertTrue(any('CSV ahead' in i for i in issues))


if __name__ == '__main__':
    unittest.main()
