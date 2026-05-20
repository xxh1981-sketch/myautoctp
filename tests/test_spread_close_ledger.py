"""spread_close_ledger unit tests"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ctp_bootstrap  # noqa: F401

from spread_ledger import SpreadLegStore
from spread_close_ledger import (
    build_positions_from_spread_claims,
    count_spread_ab_from_store,
    install_spread_close_from_ledger,
    process_close_from_spread_ledger,
)


class FakeConn:
    def __init__(self, symbol='SA', month='609'):
        self.future_prices = {symbol.lower(): 2500.0}
        self._runtime_state = {}
        self.instrument_mgr = MagicMock()
        self.instrument_mgr.lookup_option_price_tick.return_value = (0, None)

    def _normalize_month(self, symbol, month):
        return month


class TestBuildSpreadClosePositions(unittest.TestCase):

    def setUp(self):
        self.conn = FakeConn()
        self.store = SpreadLegStore()
        self.store.set_leg_claims({
            'SA609C2400': 2,
            'SA609C2500': -2,
            'SA609P2400': 1,
            'MA609C2400': 1,
        })

    def test_build_long_and_short_calls(self):
        pos = build_positions_from_spread_claims(
            self.store, self.conn, 'SA', '609',
        )
        by_inst = {p['instrument']: p for p in pos}
        self.assertEqual(by_inst['SA609C2400']['direction'], '2')
        self.assertEqual(by_inst['SA609C2400']['position'], 2)
        self.assertEqual(by_inst['SA609C2500']['direction'], '3')
        self.assertEqual(by_inst['SA609C2500']['position'], 2)
        self.assertNotIn('SA609P2400', by_inst)
        self.assertNotIn('MA609C2400', by_inst)

    def test_count_ab_from_store(self):
        a, b = count_spread_ab_from_store(self.store, self.conn, 'SA', '609')
        self.assertEqual(a, 2)
        self.assertEqual(b, 2)

    def test_empty_when_no_claims_for_symbol(self):
        pos = build_positions_from_spread_claims(
            self.store, self.conn, 'lc', '2609',
        )
        self.assertEqual(pos, [])


class TestProcessCloseFromLedger(unittest.TestCase):

    def test_skips_when_no_ledger_positions(self):
        conn = FakeConn()
        conn._runtime_state['_spread_leg_store'] = SpreadLegStore()
        item = {'future': 'SA', 'month': '609', 'vol_basis': 0.2, 'min_tick': 0.5}
        logger = MagicMock()
        with patch(
            'auto_closer_conditions.check_close_conditions_with_urgency',
        ) as mock_check:
            result = process_close_from_spread_ledger(
                conn, item, 5.0, {}, logger,
            )
        self.assertFalse(result)
        mock_check.assert_not_called()

    @patch('auto_closer_executor.execute_close_orders_with_limit', return_value=(True, 2))
    @patch('auto_closer_plan.calculate_close_plan_VIX_case', return_value=[{'A_contract': 'A'}])
    @patch('auto_closer_conditions.check_close_conditions_with_urgency', return_value=('normal', 'VIX low'))
    @patch('time.sleep')
    def test_uses_ledger_not_ctp_positions(
        self, _sleep, mock_cond, mock_plan, mock_exec,
    ):
        conn = FakeConn()
        store = SpreadLegStore()
        store.set_leg_claims({'SA609C2400': 1, 'SA609C2500': -1})
        conn._runtime_state['_spread_leg_store'] = store
        item = {'future': 'SA', 'month': '609', 'vol_basis': 0.2, 'min_tick': 0.5}
        logger = MagicMock()

        def clear_store(*args, **kwargs):
            store.set_leg_claims({})
            return (True, 2)

        mock_exec.side_effect = clear_store

        ctp_positions = [
            {'instrument': 'SA609C2400', 'direction': '2', 'position': 5},
            {'instrument': 'SA609C2500', 'direction': '3', 'position': 5},
        ]
        result = process_close_from_spread_ledger(
            conn, item, 3.0, {'VIX_EXIT_MULTIPLIER': 1.0}, logger,
            positions=ctp_positions,
        )
        self.assertTrue(result)
        plan_positions = mock_plan.call_args[0][1]
        self.assertEqual(len(plan_positions), 2)
        self.assertEqual(plan_positions[0]['position'], 1)
        self.assertEqual(plan_positions[1]['position'], 1)
        mock_cond.assert_called_once()
        cond_positions = mock_cond.call_args[0][4]
        self.assertEqual(cond_positions[0]['position'], 1)

    @patch('auto_closer_executor.execute_close_orders_with_limit', return_value=(True, 1))
    @patch('auto_closer_plan.calculate_close_plan_VIX_case', return_value=[{'A_contract': 'A'}])
    @patch('auto_closer_conditions.check_close_conditions_with_urgency', return_value=('normal', 'VIX low'))
    @patch('time.sleep')
    def test_confirm_counts_ledger_after_partial_close(
        self, _sleep, _cond, _plan, _exec,
    ):
        conn = FakeConn()
        store = SpreadLegStore()
        store.set_leg_claims({'SA609C2400': 1, 'SA609C2500': -1})
        conn._runtime_state['_spread_leg_store'] = store
        item = {'future': 'SA', 'month': '609', 'vol_basis': 0.2, 'min_tick': 0.5}
        logger = MagicMock()

        def clear_store(*args, **kwargs):
            store.set_leg_claims({})
            return (True, 1)

        _exec.side_effect = clear_store
        result = process_close_from_spread_ledger(
            conn, item, 3.0, {'VIX_EXIT_MULTIPLIER': 1.0}, logger,
        )
        self.assertTrue(result)


class TestInstallPatch(unittest.TestCase):

    def test_install_rebinds_auto_processor(self):
        import auto_closer
        import auto_processor
        import spread_close_ledger as scl

        orig = auto_closer.process_close
        try:
            scl._ORIG_PROCESS_CLOSE = None
            install_spread_close_from_ledger({
                'dual_strategy': {
                    'use_spread_leg_claims': True,
                    'spread_close_from_ledger': True,
                },
            })
            self.assertIsNot(auto_closer.process_close, orig)
            self.assertIs(auto_processor.process_close, auto_closer.process_close)
        finally:
            auto_closer.process_close = orig
            auto_processor.process_close = orig
            scl._ORIG_PROCESS_CLOSE = None


if __name__ == '__main__':
    unittest.main()
