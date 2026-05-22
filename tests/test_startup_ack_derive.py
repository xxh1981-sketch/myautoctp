"""startup ack derive / two-step flow tests"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ctp_bootstrap  # noqa: F401

from merged_startup_ack import require_startup_position_ack


def _base_config(**dual_overrides):
    dual = {
        'require_startup_ack': True,
        'startup_ack_each_run': True,
        'allow_start_on_reconcile_mismatch': False,
        'startup_ack_interactive': True,
        'startup_ack_use_gui': False,
    }
    dual.update(dual_overrides)
    return {
        'dual_strategy': dual,
        'strangle_tradeinfo': [],
        'spread_tradeinfo': [],
    }


def _mock_ledger():
    ledger = MagicMock()
    ledger.list_positions.return_value = []
    ledger.list_leg_claims.return_value = {}
    ledger.list_unmatched_legs.return_value = []
    ledger.get_daily_buy_amount.return_value = 0.0
    return ledger


class TestStartupAckTwoStep(unittest.TestCase):

    @patch('merged_startup_ack._run_account_decomposition_step', return_value=True)
    @patch('merged_startup_ack._prompt_ledger_reconcile_step', return_value='yes')
    @patch(
        'merged_startup_ack._preview_reconcile',
        return_value=(False, [], []),
    )
    @patch('merged_startup_ack._format_ctp_positions_preview', return_value='')
    def test_step1_always_runs_even_without_mismatch(
        self, _ctp, _preview, ledger_step, _decomp,
    ):
        ok = require_startup_position_ack(
            _base_config(), MagicMock(), _mock_ledger(),
            MagicMock(_runtime_state={}),
        )
        self.assertTrue(ok)
        ledger_step.assert_called_once()

    @patch('merged_startup_ack._run_account_decomposition_step', return_value=False)
    @patch('merged_startup_ack._prompt_ledger_reconcile_step', return_value='yes')
    @patch(
        'merged_startup_ack._preview_reconcile',
        return_value=(True, ['gap'], ['[对账预览] gap']),
    )
    @patch('merged_startup_ack._format_ctp_positions_preview', return_value='')
    def test_step2_blocks_when_external_not_confirmed(
        self, _ctp, _preview, _ledger_step, _decomp,
    ):
        ok = require_startup_position_ack(
            _base_config(), MagicMock(), _mock_ledger(),
            MagicMock(_runtime_state={}),
        )
        self.assertFalse(ok)
        _decomp.assert_called_once()

    @patch('merged_startup_ack._run_account_decomposition_step', return_value=True)
    @patch(
        'merged_startup_ack._prompt_ledger_reconcile_step',
        side_effect=['derive', 'yes'],
    )
    @patch('spread_derive.apply_derived_spread_from_ctp', return_value={})
    @patch(
        'merged_startup_ack._preview_reconcile',
        side_effect=[
            (False, [], []),
            (True, ['gap'], ['[对账预览] gap']),
        ],
    )
    @patch('merged_startup_ack._format_ctp_positions_preview', return_value='')
    def test_derive_loops_step1_then_step2(
        self, _ctp, _preview, _derive, ledger_step, _decomp,
    ):
        ok = require_startup_position_ack(
            _base_config(), MagicMock(), _mock_ledger(),
            MagicMock(_runtime_state={}),
        )
        self.assertTrue(ok)
        self.assertEqual(ledger_step.call_count, 2)
        _derive.assert_called_once()
        _decomp.assert_called_once()

    @patch('merged_startup_ack._prompt_ledger_reconcile_step', return_value='no')
    @patch(
        'merged_startup_ack._preview_reconcile',
        return_value=(False, [], []),
    )
    @patch('merged_startup_ack._format_ctp_positions_preview', return_value='')
    def test_step1_cancel(self, _ctp, _preview, _ledger_step):
        ok = require_startup_position_ack(
            _base_config(), MagicMock(), _mock_ledger(),
            MagicMock(_runtime_state={}),
        )
        self.assertFalse(ok)


if __name__ == '__main__':
    unittest.main()
