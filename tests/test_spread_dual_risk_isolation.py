"""Integration (real autotrade): spread executor risk/rebalance excludes strangle legs.

Reproduces the dual-strategy contamination where strangle long calls on a shared
symbol+month inflated spread A_current in ``check_risk_limits`` (false 风控阻止开仓)
and could be picked as ``rebalance_close_A_positions`` candidates (wrong close).

These need the real ``auto_executor_select`` / ``auto_rebalance`` modules, so they
run only in pytest-full (AUTOTRADE_ROOT available), not the stubbed unit suite.
"""

import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ctp_bootstrap  # noqa: F401 — injects AUTOTRADE_ROOT into sys.path

_LEDGER_CFG = {
    'dual_strategy': {
        'use_spread_leg_claims': True,
        'spread_execution_from_ledger': True,
    },
}


class TestSpreadRiskCheckExcludesStrangle(unittest.TestCase):

    def setUp(self):
        import auto_executor_select
        import spread_ledger_execution as sle

        self.aes = auto_executor_select
        self.sle = sle
        self._orig_check = auto_executor_select.RiskCheckMixin.check_risk_limits
        self._orig_flag = sle._RISK_CHECK_PATCHED

    def tearDown(self):
        self.aes.RiskCheckMixin.check_risk_limits = self._orig_check
        self.sle._RISK_CHECK_PATCHED = self._orig_flag

    def _conn_with_store(self, claims, ctp_positions):
        from spread_ledger import SpreadLegStore

        conn = MagicMock()
        conn._runtime_state = {}
        conn._normalize_month = lambda symbol, month: month
        store = SpreadLegStore()
        store.set_leg_claims(claims)
        conn._runtime_state['_spread_leg_store'] = store
        tracker = MagicMock()
        tracker.get_positions_for_symbol = MagicMock(return_value=ctp_positions)
        conn.position_tracker = tracker
        return conn

    def test_risk_check_sees_only_spread_legs(self):
        seen = {}

        def probe(self_, planned_A_groups=1, planned_B_groups=1, enforce_ratio=True):
            tr = self_.conn.position_tracker
            seen['pos'] = tr.get_positions_for_symbol(
                self_.symbol, self_.month, self_.normalized_month,
            )
            return True, 'ok'

        self.aes.RiskCheckMixin.check_risk_limits = probe
        self.sle._RISK_CHECK_PATCHED = False
        self.sle.install_spread_risk_check_exclusion(_LEDGER_CFG)

        conn = self._conn_with_store(
            {'MA609C3700': -10, 'MA609C3500': 5},
            [
                {'instrument': 'MA609C3650', 'direction': '2', 'position': 20},
                {'instrument': 'MA609C3500', 'direction': '2', 'position': 5},
                {'instrument': 'MA609C3700', 'direction': '3', 'position': 10},
            ],
        )
        ex = self.aes.RiskCheckMixin()
        ex.conn = conn
        ex.config = _LEDGER_CFG
        ex.symbol = 'MA'
        ex.month = '609'
        ex.normalized_month = '609'

        ok, _reason = ex.check_risk_limits()
        self.assertTrue(ok)
        insts = {p['instrument'] for p in seen['pos']}
        self.assertNotIn('MA609C3650', insts)
        self.assertIn('MA609C3500', insts)
        self.assertIn('MA609C3700', insts)

    def test_tracker_method_restored_after_call(self):
        def probe(self_, *a, **kw):
            return True, 'ok'

        self.aes.RiskCheckMixin.check_risk_limits = probe
        self.sle._RISK_CHECK_PATCHED = False
        self.sle.install_spread_risk_check_exclusion(_LEDGER_CFG)

        conn = self._conn_with_store(
            {'MA609C3500': 5},
            [{'instrument': 'MA609C3500', 'direction': '2', 'position': 5}],
        )
        before = conn.position_tracker.get_positions_for_symbol
        ex = self.aes.RiskCheckMixin()
        ex.conn = conn
        ex.config = _LEDGER_CFG
        ex.symbol = 'MA'
        ex.month = '609'
        ex.normalized_month = '609'
        ex.check_risk_limits()
        self.assertIs(conn.position_tracker.get_positions_for_symbol, before)


class TestRebalanceCloseAExcludesStrangle(unittest.TestCase):

    def setUp(self):
        import auto_rebalance
        import spread_ledger_execution as sle

        self.ar = auto_rebalance
        self.sle = sle
        self._orig_fn = auto_rebalance.rebalance_close_A_positions
        self._orig_qpf = auto_rebalance.query_positions_fallback
        self._orig_flag = sle._REBALANCE_CLOSE_A_PATCHED

    def tearDown(self):
        self.ar.rebalance_close_A_positions = self._orig_fn
        self.ar.query_positions_fallback = self._orig_qpf
        self.sle._REBALANCE_CLOSE_A_PATCHED = self._orig_flag

    def test_close_a_candidates_exclude_strangle(self):
        from spread_ledger import SpreadLegStore

        seen = {}

        def probe(conn, analysis, symbol, month, min_tick, cfg, logger, vol_of_combo):
            seen['pos'] = self.ar.query_positions_fallback(
                conn, logger=logger, symbol=symbol,
            )
            return True

        self.ar.rebalance_close_A_positions = probe
        self.sle._REBALANCE_CLOSE_A_PATCHED = False
        self.sle.install_spread_rebalance_close_a_exclusion(_LEDGER_CFG)

        conn = MagicMock()
        conn._runtime_state = {}
        conn._normalize_month = lambda symbol, month: month
        store = SpreadLegStore()
        store.set_leg_claims({'MA609C3500': 5})
        conn._runtime_state['_spread_leg_store'] = store
        conn.query_positions_sync = MagicMock(return_value=[
            {'instrument': 'MA609C3650', 'direction': '2', 'position': 20},
            {'instrument': 'MA609C3500', 'direction': '2', 'position': 5},
        ])

        self.ar.rebalance_close_A_positions(
            conn, {}, 'MA', '609', 0.5, _LEDGER_CFG, MagicMock(), 1,
        )
        insts = {p['instrument'] for p in seen['pos']}
        self.assertNotIn('MA609C3650', insts)
        self.assertIn('MA609C3500', insts)
        self.assertIs(self.ar.query_positions_fallback, self._orig_qpf)


if __name__ == '__main__':
    unittest.main()
