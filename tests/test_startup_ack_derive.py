"""startup ack derive / mismatch dialog tests"""

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


class TestStartupAckDerive(unittest.TestCase):

    @patch('merged_startup_ack._prompt_interactive_ack', return_value='derive')
    @patch('merged_startup_ack._prompt_reconcile_mismatch_ack', return_value='no')
    @patch('spread_derive.apply_derived_spread_from_ctp', return_value={})
    @patch(
        'merged_startup_ack._preview_reconcile',
        side_effect=[(False, [], []), (True, ['gap'], ['[对账预览] gap'])],
    )
    @patch('merged_startup_ack._format_ctp_positions_preview', return_value='')
    def test_derive_mismatch_user_cancels(
        self, _ctp, _preview, _derive, _mismatch_prompt, _prompt,
    ):
        ok = require_startup_position_ack(
            _base_config(), MagicMock(), _mock_ledger(), MagicMock(_runtime_state={}),
        )
        self.assertFalse(ok)
        _mismatch_prompt.assert_called_once()
        call_kw = _mismatch_prompt.call_args[1]
        self.assertEqual(call_kw.get('context'), '推导后对账')
        self.assertFalse(call_kw.get('allow_derive'))

    @patch('merged_startup_ack._prompt_interactive_ack', return_value='derive')
    @patch('merged_startup_ack._prompt_reconcile_mismatch_ack', return_value='yes')
    @patch('spread_derive.apply_derived_spread_from_ctp', return_value={})
    @patch(
        'merged_startup_ack._preview_reconcile',
        side_effect=[(False, [], []), (True, ['gap'], [])],
    )
    @patch('merged_startup_ack._format_ctp_positions_preview', return_value='')
    def test_derive_mismatch_user_confirms(
        self, _ctp, _preview, _derive, _mismatch_prompt, _prompt,
    ):
        ok = require_startup_position_ack(
            _base_config(), MagicMock(), _mock_ledger(), MagicMock(_runtime_state={}),
        )
        self.assertTrue(ok)

    @patch('merged_startup_ack._prompt_interactive_ack')
    @patch('merged_startup_ack._prompt_reconcile_mismatch_ack', return_value='yes')
    @patch(
        'merged_startup_ack._preview_reconcile',
        return_value=(True, ['SA609C1000 gap=1'], ['[对账预览] gap']),
    )
    @patch('merged_startup_ack._format_ctp_positions_preview', return_value='')
    def test_initial_mismatch_shows_mismatch_dialog(
        self, _ctp, _preview, _mismatch_prompt, _interactive,
    ):
        ok = require_startup_position_ack(
            _base_config(), MagicMock(), _mock_ledger(), MagicMock(_runtime_state={}),
        )
        self.assertTrue(ok)
        _mismatch_prompt.assert_called_once()
        _interactive.assert_not_called()


if __name__ == '__main__':
    unittest.main()
