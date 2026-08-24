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
    maybe_alert_hv_staleness,
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


class TestHvStalenessAlert(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.hv_path = os.path.join(self.tmp.name, 'futures.xlsx')
        with open(self.hv_path, 'wb') as f:
            f.write(b'stub')
        self.conn = _conn()

    def _config(self, **overrides):
        str_cfg = {
            'hv_close_path': self.hv_path,
            'hv_stale_alert_enabled': True,
            'hv_stale_warn_days': 7,
            'hv_stale_check_interval_sec': 0,
            'hv_stale_alert_cooldown_sec': 0,
        }
        str_cfg.update(overrides)
        return {'strangle': str_cfg}

    def _set_age_days(self, days: float) -> None:
        ts = time.time() - days * 86400
        os.utime(self.hv_path, (ts, ts))

    def test_fresh_file_no_alert(self):
        self._set_age_days(1)
        with patch('auto_feishu.send_feishu_message', return_value=True) as m:
            self.assertFalse(maybe_alert_hv_staleness(self.conn, self._config()))
        m.assert_not_called()

    def test_stale_file_alerts(self):
        self._set_age_days(10)
        with patch('auto_feishu.send_feishu_message', return_value=True) as m:
            self.assertTrue(maybe_alert_hv_staleness(self.conn, self._config()))
        m.assert_called_once()
        self.assertIn('HV 收盘价库', m.call_args.args[0])

    def test_missing_file_alerts(self):
        os.remove(self.hv_path)
        with patch('auto_feishu.send_feishu_message', return_value=True) as m:
            self.assertTrue(maybe_alert_hv_staleness(self.conn, self._config()))
        m.assert_called_once()
        self.assertIn('不存在', m.call_args.args[0])

    def test_cooldown_suppresses_repeat(self):
        self._set_age_days(10)
        cfg = self._config(hv_stale_alert_cooldown_sec=3600)
        with patch('auto_feishu.send_feishu_message', return_value=True) as m:
            self.assertTrue(maybe_alert_hv_staleness(self.conn, cfg))
            self.assertFalse(maybe_alert_hv_staleness(self.conn, cfg))
        self.assertEqual(m.call_count, 1)

    def test_disabled_no_alert(self):
        self._set_age_days(10)
        cfg = self._config(hv_stale_alert_enabled=False)
        with patch('auto_feishu.send_feishu_message', return_value=True) as m:
            self.assertFalse(maybe_alert_hv_staleness(self.conn, cfg))
        m.assert_not_called()

    def test_warn_days_zero_disables(self):
        self._set_age_days(10)
        cfg = self._config(hv_stale_warn_days=0)
        with patch('auto_feishu.send_feishu_message', return_value=True) as m:
            self.assertFalse(maybe_alert_hv_staleness(self.conn, cfg))
        m.assert_not_called()

    def test_recovery_resets_cooldown(self):
        # 过期告警一次 → 用户更新文件（变新）→ 再过期时应能再次告警。
        self._set_age_days(10)
        cfg = self._config(hv_stale_alert_cooldown_sec=999999)
        with patch('auto_feishu.send_feishu_message', return_value=True) as m:
            self.assertTrue(maybe_alert_hv_staleness(self.conn, cfg))
            self._set_age_days(1)
            self.assertFalse(maybe_alert_hv_staleness(self.conn, cfg))
            self._set_age_days(10)
            self.assertTrue(maybe_alert_hv_staleness(self.conn, cfg))
        self.assertEqual(m.call_count, 2)

    def test_send_failure_returns_false(self):
        self._set_age_days(10)
        with patch('auto_feishu.send_feishu_message', side_effect=RuntimeError('down')):
            self.assertFalse(maybe_alert_hv_staleness(self.conn, self._config()))


if __name__ == '__main__':
    unittest.main()
