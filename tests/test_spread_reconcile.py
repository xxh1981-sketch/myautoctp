"""spread_reconcile integration tests.

纯逻辑（signed 行、Call 过滤、宽跨互扣、grace 窗口等）见 ``test_spread_reconcile_unit.py``。
本文件仅保留依赖 ``StrangleLedger`` 的双策略集成场景。
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ctp_bootstrap  # noqa: F401

from spread_reconcile import reconcile_spread_positions
from spread_ledger import SpreadLegStore


class FakeConn:
    def __init__(self):
        self._runtime_state = {}

    def _normalize_month(self, symbol, month):
        return month


class TestSpreadReconcileStrangleIntegration(unittest.TestCase):

    def test_reconcile_no_halt_when_strangle_owns_excess_call(self):
        from straggle_ledger import StrangleLedger

        conn = FakeConn()
        store = SpreadLegStore()
        store.set_leg_claims({'SA609C2400': 1})
        conn._runtime_state['_spread_leg_store'] = store

        with tempfile.TemporaryDirectory() as tmp:
            ledger = StrangleLedger(os.path.join(tmp, 'sl.json'))
            ledger.set_leg_claims({'SA609C2400': 2})
            conn._runtime_state['_strangle_ledger'] = ledger

            conn.query_positions_sync = lambda timeout=10: [
                {'instrument': 'SA609C2400', 'direction': '2', 'position': 3},
            ]
            tradeinfo = [{'future': 'SA', 'month': '609'}]
            cfg = {'dual_strategy': {'auto_sync_spread_positions_csv': False}}
            halt, issues = reconcile_spread_positions(
                conn, tradeinfo, None, config=cfg,
            )
            self.assertFalse(halt)
            self.assertEqual(issues, [])

    def test_reconcile_halt_when_disabled_subtraction(self):
        from straggle_ledger import StrangleLedger

        conn = FakeConn()
        store = SpreadLegStore()
        store.set_leg_claims({'SA609C2400': 1})
        conn._runtime_state['_spread_leg_store'] = store

        with tempfile.TemporaryDirectory() as tmp:
            ledger = StrangleLedger(os.path.join(tmp, 'sl.json'))
            ledger.set_leg_claims({'SA609C2400': 2})
            conn._runtime_state['_strangle_ledger'] = ledger

            conn.query_positions_sync = lambda timeout=10: [
                {'instrument': 'SA609C2400', 'direction': '2', 'position': 3},
            ]
            tradeinfo = [{'future': 'SA', 'month': '609'}]
            cfg = {
                'dual_strategy': {
                    'auto_sync_spread_positions_csv': False,
                    'exclude_strangle_from_spread_reconcile': False,
                },
            }
            halt, issues = reconcile_spread_positions(
                conn, tradeinfo, None, config=cfg,
            )
            self.assertTrue(halt)
            self.assertTrue(issues)


if __name__ == '__main__':
    unittest.main()
