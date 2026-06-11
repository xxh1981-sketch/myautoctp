"""unattended_observability：配置 drift 告警 + halt 解除通知。"""

import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import autotrade_stubs

autotrade_stubs.ensure_auto_feishu_stub()

from unattended_observability import (
    init_config_drift_baseline,
    maybe_alert_config_drift,
    maybe_notify_halt_recoveries,
)


def _conn():
    from unittest.mock import MagicMock
    conn = MagicMock()
    conn._runtime_state = {}
    return conn


class TestConfigDriftAlert(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg_path = os.path.join(self.tmp.name, 'merged_config.yaml')
        with open(self.cfg_path, 'w', encoding='utf-8') as f:
            f.write('loop_interval: 10\n')
        self.conn = _conn()
        self.config = {
            'config_drift_alert_enabled': True,
            'config_drift_check_interval_sec': 0,
        }

    @patch('merged_config.merged_config_local_path')
    def test_no_alert_when_unchanged(self, mock_path):
        mock_path.return_value = self.cfg_path
        init_config_drift_baseline(self.conn, self.config)
        with patch('auto_feishu.send_feishu_message', return_value=True) as m:
            self.assertFalse(maybe_alert_config_drift(self.conn, self.config))
        m.assert_not_called()

    @patch('merged_config.merged_config_local_path')
    def test_alert_when_mtime_newer_than_baseline(self, mock_path):
        mock_path.return_value = self.cfg_path
        init_config_drift_baseline(self.conn, self.config)
        time.sleep(0.05)
        with open(self.cfg_path, 'a', encoding='utf-8') as f:
            f.write('# touch\n')
        with patch('auto_feishu.send_feishu_message', return_value=True) as m:
            self.assertTrue(maybe_alert_config_drift(self.conn, self.config))
        m.assert_called_once()
        self.assertIn('未重启', m.call_args.args[0])

    @patch('merged_config.merged_config_local_path')
    def test_same_change_not_spammed(self, mock_path):
        mock_path.return_value = self.cfg_path
        init_config_drift_baseline(self.conn, self.config)
        time.sleep(0.05)
        with open(self.cfg_path, 'a', encoding='utf-8') as f:
            f.write('# touch\n')
        with patch('auto_feishu.send_feishu_message', return_value=True) as m:
            self.assertTrue(maybe_alert_config_drift(self.conn, self.config))
            self.assertFalse(maybe_alert_config_drift(self.conn, self.config))
        self.assertEqual(m.call_count, 1)


class TestHaltRecoveryNotify(unittest.TestCase):

    def setUp(self):
        self.conn = _conn()
        self.config = {
            'halt_recovery_notify_enabled': True,
            'halt_recovery_alert_cooldown_sec': 0,
        }

    def test_first_round_initializes_without_notify(self):
        self.conn._runtime_state['_margin_halt_open'] = True
        with patch('auto_feishu.send_feishu_message', return_value=True) as m:
            self.assertFalse(maybe_notify_halt_recoveries(self.conn, self.config))
        m.assert_not_called()

    def test_notify_when_halt_clears(self):
        runtime = self.conn._runtime_state
        runtime['_halt_notify_prev'] = {
            'margin': True,
            'journal': False,
            'spread_reconcile': False,
            'strangle_reconcile': False,
        }
        runtime['_margin_halt_open'] = False
        with patch('auto_feishu.send_feishu_message', return_value=True) as m:
            self.assertTrue(maybe_notify_halt_recoveries(self.conn, self.config))
        m.assert_called_once()
        self.assertIn('保证金 halt', m.call_args.args[0])

    def test_no_notify_when_still_halted(self):
        runtime = self.conn._runtime_state
        runtime['_halt_notify_prev'] = {'margin': True, 'journal': False,
                                        'spread_reconcile': False, 'strangle_reconcile': False}
        runtime['_margin_halt_open'] = True
        with patch('auto_feishu.send_feishu_message', return_value=True) as m:
            self.assertFalse(maybe_notify_halt_recoveries(self.conn, self.config))
        m.assert_not_called()


if __name__ == '__main__':
    unittest.main()
