"""merged_startup_ack unit tests"""

import os
import sys
import tempfile
import unittest
from datetime import date, timedelta
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ctp_bootstrap  # noqa: F401

from merged_startup_ack import (
    _file_ack_ok,
    _preview_reconcile,
    _should_prefer_gui,
    require_startup_position_ack,
)


class TestStartupAckGui(unittest.TestCase):

    def test_prefer_gui_default_even_with_tty(self):
        dual = {'startup_ack_use_gui': True}
        self.assertTrue(_should_prefer_gui(dual))

    def test_force_terminal_disables_gui(self):
        dual = {
            'startup_ack_use_gui': True,
            'startup_ack_prefer_gui': True,
            'startup_ack_force_terminal': True,
        }
        self.assertFalse(_should_prefer_gui(dual))


class TestPreviewReconcile(unittest.TestCase):

    def test_preview_delegates_dual(self):
        conn = MagicMock()
        ledger = MagicMock()
        ledger.list_leg_claims.return_value = {}
        config = {
            'strangle_tradeinfo': [{'future': 'ma', 'month': '2609'}],
            'spread_tradeinfo': [],
            'dual_strategy': {'exclude_spread_from_strangle_reconcile': True},
            'strangle': {'auto_sync_positions_csv': False},
        }
        conn.query_positions_sync.return_value = []
        conn._runtime_state = {}
        with unittest.mock.patch(
            'strangle_reconcile_dual.reconcile_strangle_positions_dual',
            return_value=(False, []),
        ) as mock_fn:
            halt, issues, lines = _preview_reconcile(conn, ledger, config, None)
        self.assertFalse(halt)
        mock_fn.assert_called_once()


class TestStartupAckFile(unittest.TestCase):

    def test_file_ack_ok_without_require_today_accepts_yesterday(self):
        with tempfile.TemporaryDirectory() as tmp:
            ack_file = os.path.join(tmp, 'position_startup_ack.txt')
            yesterday = (date.today() - timedelta(days=1)).isoformat()
            with open(ack_file, 'w', encoding='utf-8') as f:
                f.write(f'confirmed {yesterday}\n')
            config = {
                'dual_strategy': {
                    'startup_ack_file': ack_file,
                    'startup_ack_require_today': False,
                },
            }
            self.assertTrue(_file_ack_ok(config, require_today=False))

    def test_file_ack_ok_with_require_today_rejects_yesterday(self):
        with tempfile.TemporaryDirectory() as tmp:
            ack_file = os.path.join(tmp, 'position_startup_ack.txt')
            yesterday = (date.today() - timedelta(days=1)).isoformat()
            with open(ack_file, 'w', encoding='utf-8') as f:
                f.write(f'confirmed {yesterday}\n')
            config = {'dual_strategy': {'startup_ack_file': ack_file}}
            self.assertFalse(_file_ack_ok(config, require_today=True))

    def test_require_startup_ack_skips_interactive_on_auto_restart_with_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            ack_file = os.path.join(tmp, 'position_startup_ack.txt')
            with open(ack_file, 'w', encoding='utf-8') as f:
                f.write(f'confirmed {date.today().isoformat()}\n')
            config = {
                '_manual_start': False,
                '_auto_restart': True,
                'dual_strategy': {
                    'require_startup_ack': True,
                    'startup_ack_each_run': False,
                    'startup_ack_require_today': False,
                    'startup_ack_file': ack_file,
                },
            }
            ledger = MagicMock()
            ledger.list_positions.return_value = []
            ledger.list_leg_claims.return_value = {}
            ledger.list_unmatched_legs.return_value = []
            ledger.get_daily_buy_amount.return_value = 0.0
            logger = MagicMock()
            ok = require_startup_position_ack(config, logger, ledger, conn=None)
            self.assertTrue(ok)
            logger.info.assert_any_call(f'[启动] 持仓已确认: {ack_file}')

    def test_manual_start_ignores_ack_file_and_prompts(self):
        with tempfile.TemporaryDirectory() as tmp:
            ack_file = os.path.join(tmp, 'position_startup_ack.txt')
            with open(ack_file, 'w', encoding='utf-8') as f:
                f.write(f'confirmed {date.today().isoformat()}\n')
            config = {
                '_manual_start': True,
                'dual_strategy': {
                    'require_startup_ack': True,
                    'startup_ack_each_run': False,
                    'startup_ack_interactive': True,
                    'startup_ack_use_gui': False,
                    'startup_ack_file': ack_file,
                },
            }
            ledger = MagicMock()
            ledger.list_positions.return_value = []
            ledger.list_leg_claims.return_value = {}
            ledger.list_unmatched_legs.return_value = []
            ledger.get_daily_buy_amount.return_value = 0.0
            logger = MagicMock()
            with unittest.mock.patch(
                'merged_startup_ack._prompt_interactive_ack',
                return_value='yes',
            ) as mock_prompt:
                ok = require_startup_position_ack(config, logger, ledger, conn=None)
            self.assertTrue(ok)
            mock_prompt.assert_called_once()
            logged = ' '.join(str(c) for c in logger.info.call_args_list)
            self.assertNotIn('持仓已确认', logged)


if __name__ == '__main__':
    unittest.main()
