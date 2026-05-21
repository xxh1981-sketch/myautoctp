"""merged_startup_checks unit tests。"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import merged_startup_checks as msc


class TestApplyStartupMargin(unittest.TestCase):

    def setUp(self):
        self.conn = MagicMock()
        self.conn._runtime_state = {}
        self.logger = MagicMock()
        self.ledger = MagicMock()
        self.str_cfg = {'pause_open_on_reconcile_mismatch': True}

    @patch('merged_startup_checks.check_margin_status', return_value=('ok', ''))
    def test_ok_clears_margin_halt(self, _mock):
        self.assertTrue(msc.apply_startup_margin(
            self.conn, {'global_margin_limit': 100000},
            self.logger, self.ledger, self.str_cfg,
        ))
        self.assertFalse(self.conn._runtime_state['_margin_halt_open'])

    @patch('merged_startup_checks._notify_startup_margin')
    @patch('merged_startup_checks.check_margin_status', return_value=('over_limit', '超限'))
    def test_over_limit_sets_halt_and_allows_startup(self, _mock, _notify):
        self.assertTrue(msc.apply_startup_margin(
            self.conn, {'global_margin_limit': 100000},
            self.logger, self.ledger, self.str_cfg,
        ))
        self.assertTrue(self.conn._runtime_state['_margin_halt_open'])
        self.assertIn('限额', self.conn._runtime_state['_margin_halt_reason'])
        _notify.assert_called_once()

    @patch('merged_startup_checks._notify_startup_margin')
    @patch(
        'merged_startup_checks.check_margin_status',
        return_value=('unknown', '持仓查询失败'),
    )
    def test_unknown_notifies_when_limit_enabled(self, _mock, notify):
        msc.apply_startup_margin(
            self.conn, {'global_margin_limit': 100000},
            self.logger, self.ledger, self.str_cfg,
        )
        notify.assert_called_once()

    @patch(
        'merged_startup_checks.check_margin_status',
        return_value=('unknown', '持仓查询失败'),
    )
    def test_unknown_sets_conservative_halt_when_limit_enabled(self, _mock):
        self.assertTrue(msc.apply_startup_margin(
            self.conn, {'global_margin_limit': 100000},
            self.logger, self.ledger, self.str_cfg,
        ))
        self.assertTrue(self.conn._runtime_state['_margin_halt_open'])

    @patch(
        'merged_startup_checks.check_margin_status',
        return_value=('unknown', '持仓查询失败'),
    )
    def test_unknown_no_halt_when_limit_disabled(self, _mock):
        self.assertTrue(msc.apply_startup_margin(
            self.conn, {'global_margin_limit': 0},
            self.logger, self.ledger, self.str_cfg,
        ))
        self.assertNotIn('_margin_halt_open', self.conn._runtime_state)


class TestAuditTargetMonths(unittest.TestCase):

    @patch('order_whitelist_guard.audit_target_months_coverage', return_value=['io'])
    @patch('merged_startup_checks.sys.exit')
    def test_fail_fast_exits(self, mock_exit, _mock_audit):
        conn = MagicMock()
        msc.audit_target_months(
            conn,
            {'fail_fast_on_empty_target_months': True},
            MagicMock(),
            [], [],
            send_feishu=lambda *a, **k: None,
        )
        mock_exit.assert_called_once_with(5)

    @patch('order_whitelist_guard.audit_target_months_coverage', return_value=[])
    @patch('merged_startup_checks.sys.exit')
    def test_no_missing_does_not_exit(self, mock_exit, _mock_audit):
        conn = MagicMock()
        msc.audit_target_months(
            conn, {}, MagicMock(), [], [],
            send_feishu=lambda *a, **k: None,
        )
        mock_exit.assert_not_called()


if __name__ == '__main__':
    unittest.main()
