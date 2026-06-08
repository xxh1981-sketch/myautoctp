"""spread_reconcile 核心逻辑 unit tests（stub autotrade，不含 StrangleLedger 集成）。"""

import os
import sys
import time
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import autotrade_stubs

autotrade_stubs.ensure_autotrade_stubs(['auto_connection', 'auto_connection_utils', 'auto_position'])
import ctp_bootstrap  # noqa: F401 — pytest-full 注入 AUTOTRADE_ROOT

from spread_reconcile import (
    _previous_halt_state,
    _signed_from_position_row,
    _spread_symbol_months,
    _strangle_long_calls_for_spread,
    ctp_spread_signed_claims,
    reconcile_spread_positions,
    SPREAD_TRANSIENT_STREAK_KEY,
)
from spread_ledger import SpreadLegStore


class FakeStrangleLedger:
    def __init__(self, claims=None, unmatched=None):
        self._claims = claims or {}
        self._unmatched = unmatched or []

    def list_leg_claims(self):
        return dict(self._claims)

    def list_unmatched_legs(self):
        return list(self._unmatched)


class FakeConn:
    def __init__(self):
        self._runtime_state = {}

    def _normalize_month(self, symbol, month):
        return month


class TestSpreadReconcilePure(unittest.TestCase):

    def test_spread_symbol_months(self):
        keys = _spread_symbol_months([
            {'future': 'SA', 'month': '609'},
            {'future': 'ma', 'month': '608'},
        ])
        self.assertEqual(keys, {('sa', '609'), ('ma', '608')})

    def test_signed_row_long_short(self):
        _, v = _signed_from_position_row(
            {'instrument': 'SA609C2400', 'direction': '2', 'position': 2},
        )
        self.assertEqual(v, 2)
        _, v = _signed_from_position_row(
            {'instrument': 'SA609C2500', 'direction': '3', 'position': 1},
        )
        self.assertEqual(v, -1)

    def test_ctp_spread_claims_call_only(self):
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

    def test_ctp_claims_subtract_strangle_long_calls(self):
        conn = FakeConn()
        tradeinfo = [{'future': 'SA', 'month': '609'}]
        positions = [
            {'instrument': 'SA609C2400', 'direction': '2', 'position': 3},
            {'instrument': 'SA609C2500', 'direction': '3', 'position': 2},
        ]
        claims = ctp_spread_signed_claims(
            conn, tradeinfo, positions,
            strangle_long_calls={'SA609C2400': 2},
        )
        self.assertEqual(claims['SA609C2400'], 1)
        self.assertEqual(claims['SA609C2500'], -2)

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

    def test_previous_halt_state_fallback(self):
        conn = FakeConn()
        conn._runtime_state['_spread_reconcile_halt'] = True
        conn._runtime_state['_spread_reconcile_issues'] = ['old']
        prev_halt, prev_issues = _previous_halt_state(conn)
        self.assertTrue(prev_halt)
        self.assertEqual(prev_issues, ['old'])

    def test_grace_window_suppresses_halt(self):
        conn = FakeConn()
        store = SpreadLegStore()
        store.set_leg_claims({'SA609C2400': 1})
        conn._runtime_state['_spread_leg_store'] = store
        conn._runtime_state['_reconcile_grace_until'] = time.time() + 60
        conn.query_positions_sync = lambda timeout=10: [
            {'instrument': 'SA609C2400', 'direction': '2', 'position': 2},
        ]
        tradeinfo = [{'future': 'SA', 'month': '609'}]
        cfg = {'dual_strategy': {'auto_sync_spread_positions_csv': False}}
        halt, issues = reconcile_spread_positions(
            conn, tradeinfo, None, config=cfg,
        )
        self.assertFalse(halt)
        self.assertTrue(issues)

    def test_transient_query_failure_uses_prev_halt_once(self):
        conn = FakeConn()
        store = SpreadLegStore()
        conn._runtime_state['_spread_leg_store'] = store
        conn.query_positions_sync = MagicMock(side_effect=RuntimeError('timeout'))
        tradeinfo = [{'future': 'SA', 'month': '609'}]
        cfg = {
            'dual_strategy': {
                'auto_sync_spread_positions_csv': False,
                'reconcile_transient_halt_after': 3,
            },
        }
        halt, _ = reconcile_spread_positions(conn, tradeinfo, None, config=cfg)
        self.assertFalse(halt)
        self.assertEqual(conn._runtime_state.get(SPREAD_TRANSIENT_STREAK_KEY), 1)

    def test_transient_query_failure_forces_halt_after_threshold(self):
        conn = FakeConn()
        store = SpreadLegStore()
        conn._runtime_state['_spread_leg_store'] = store
        conn.query_positions_sync = MagicMock(side_effect=RuntimeError('timeout'))
        tradeinfo = [{'future': 'SA', 'month': '609'}]
        cfg = {
            'dual_strategy': {
                'auto_sync_spread_positions_csv': False,
                'reconcile_transient_halt_after': 2,
            },
        }
        reconcile_spread_positions(conn, tradeinfo, None, config=cfg)
        halt, issues = reconcile_spread_positions(conn, tradeinfo, None, config=cfg)
        self.assertTrue(halt)
        self.assertTrue(any('连续 2 次 transient' in i for i in issues))


class TestStrangleLongCallsForSpread(unittest.TestCase):
    """对账侧宽跨多头扣减：含 leg_claims + 未配对腿，排除 awaiting_phase2，仅 Call。"""

    def test_none_ledger_returns_empty(self):
        conn = FakeConn()
        self.assertEqual(_strangle_long_calls_for_spread(conn), {})

    def test_leg_claims_calls_only(self):
        conn = FakeConn()
        conn._runtime_state['_strangle_ledger'] = FakeStrangleLedger(
            claims={'SA609C2400': 2, 'SA609P1900': 5},
        )
        out = _strangle_long_calls_for_spread(conn)
        self.assertEqual(out['SA609C2400'], 2)
        self.assertNotIn('SA609P1900', out)

    def test_includes_unmatched_legs(self):
        conn = FakeConn()
        conn._runtime_state['_strangle_ledger'] = FakeStrangleLedger(
            claims={'SA609C2400': 1},
            unmatched=[{'filled_instrument': 'MA609C3650', 'volume': 20}],
        )
        out = _strangle_long_calls_for_spread(conn)
        self.assertEqual(out['SA609C2400'], 1)
        self.assertEqual(out['MA609C3650'], 20)

    def test_awaiting_phase2_not_double_counted(self):
        conn = FakeConn()
        conn._runtime_state['_strangle_ledger'] = FakeStrangleLedger(
            claims={'RM609C2750': 29},
            unmatched=[{
                'kind': 'awaiting_phase2',
                'filled_instrument': 'RM609C2750',
                'leg': {'inst': 'RM609P2025'},
                'volume': 8,
            }],
        )
        out = _strangle_long_calls_for_spread(conn)
        self.assertEqual(out['RM609C2750'], 29)

    def test_preserves_lowercase_symbol_case(self):
        conn = FakeConn()
        conn._runtime_state['_strangle_ledger'] = FakeStrangleLedger(
            unmatched=[{'filled_instrument': 'm2609-C-3400', 'volume': 50}],
        )
        out = _strangle_long_calls_for_spread(conn)
        self.assertEqual(out['m2609-C-3400'], 50)


if __name__ == '__main__':
    unittest.main()
