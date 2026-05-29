"""margin_check unit tests（不依赖 ctp_bootstrap）。"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import autotrade_stubs

autotrade_stubs.ensure_autotrade_stubs(['auto_risk'])

import margin_check


class TestCheckMarginStatus(unittest.TestCase):

    def setUp(self):
        self.conn = MagicMock()
        self.conn._runtime_state = {}
        self.logger = MagicMock()

    def test_limit_disabled_returns_ok(self):
        cfg = {'global_margin_limit': 0}
        status, reason = margin_check.check_margin_status(self.conn, cfg, self.logger)
        self.assertEqual(status, 'ok')
        self.assertEqual(reason, '')
        self.assertTrue(self.conn._runtime_state.get('_margin_disabled_warned'))

    def test_limit_disabled_warns_once(self):
        cfg = {'global_margin_limit': 0}
        margin_check.check_margin_status(self.conn, cfg, self.logger)
        margin_check.check_margin_status(self.conn, cfg, self.logger)
        warning_calls = [
            c for c in self.logger.warning.call_args_list
            if 'global_margin_limit=0' in str(c)
        ]
        self.assertEqual(len(warning_calls), 1)

    def test_under_limit_returns_ok(self):
        cfg = {'global_margin_limit': 100000, 'margin_check_max_attempts': 3}
        self.conn.query_positions_sync.return_value = [MagicMock()]
        with patch('auto_risk.sum_positions_margin_for_limit', return_value=(50000, {})):
            status, reason = margin_check.check_margin_status(
                self.conn, cfg, self.logger,
            )
        self.assertEqual(status, 'ok')
        self.assertEqual(reason, '')

    def test_over_limit(self):
        cfg = {'global_margin_limit': 100000, 'margin_check_max_attempts': 3}
        self.conn.query_positions_sync.return_value = [MagicMock()]
        with patch('auto_risk.sum_positions_margin_for_limit', return_value=(150000, {})):
            status, reason = margin_check.check_margin_status(
                self.conn, cfg, self.logger,
            )
        self.assertEqual(status, 'over_limit')
        self.assertIn('保证金超限', reason)

    def test_unknown_when_position_query_fails(self):
        cfg = {
            'global_margin_limit': 100000,
            'margin_check_max_attempts': 3,
            'margin_retry_interval': 0,
        }
        self.conn.query_positions_sync.return_value = None
        with patch('time.sleep'):
            status, reason = margin_check.check_margin_status(
                self.conn, cfg, self.logger,
            )
        self.assertEqual(status, 'unknown')
        self.assertIn('持仓查询失败', reason)

    def test_max_attempts_override_single_attempt_no_sleep(self):
        # 主循环周期路径：max_attempts=1 → 仅查 1 次、绝不 sleep（即便配置 retry=30）。
        cfg = {
            'global_margin_limit': 100000,
            'margin_check_max_attempts': 3,
            'margin_retry_interval': 30,
        }
        self.conn.query_positions_sync.return_value = None
        import time as _t
        with patch.object(_t, 'sleep') as mock_sleep:
            status, reason = margin_check.check_margin_status(
                self.conn, cfg, self.logger, context='主循环', max_attempts=1,
            )
        self.assertEqual(status, 'unknown')
        self.assertEqual(self.conn.query_positions_sync.call_count, 1)
        mock_sleep.assert_not_called()

    def test_retry_interval_override(self):
        # 覆盖 retry_interval：失败重试间隔用入参而非配置的 30s。
        cfg = {
            'global_margin_limit': 100000,
            'margin_check_max_attempts': 2,
            'margin_retry_interval': 30,
        }
        self.conn.query_positions_sync.return_value = None
        import time as _t
        with patch.object(_t, 'sleep') as mock_sleep:
            margin_check.check_margin_status(
                self.conn, cfg, self.logger, retry_interval=0,
            )
        # 2 次尝试之间 sleep 一次，且用覆盖值 0
        mock_sleep.assert_called_once_with(0)


class TestCheckMarginLegacyWrapper(unittest.TestCase):

    def setUp(self):
        self.conn = MagicMock()
        self.logger = MagicMock()

    def test_ok_returns_true(self):
        with patch.object(
            margin_check, 'check_margin_status', return_value=('ok', ''),
        ):
            self.assertTrue(margin_check.check_margin(self.conn, {}, self.logger))

    def test_unknown_returns_false_backward_compat(self):
        with patch.object(
            margin_check, 'check_margin_status', return_value=('unknown', 'y'),
        ):
            self.assertFalse(margin_check.check_margin(self.conn, {}, self.logger))


if __name__ == '__main__':
    unittest.main()
