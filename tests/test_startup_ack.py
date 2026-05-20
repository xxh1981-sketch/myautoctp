"""merged_startup_ack unit tests"""

import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ctp_bootstrap  # noqa: F401

from merged_startup_ack import _preview_reconcile, _should_prefer_gui


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


if __name__ == '__main__':
    unittest.main()
