"""check_heartbeat_watchdog 单元测试（不发送真实飞书）。"""

import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import autotrade_stubs

autotrade_stubs.ensure_auto_feishu_stub()


class TestHeartbeatWatchdog(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.hb = os.path.join(self.tmp.name, 'hb.txt')
        self.marker = os.path.join(self.tmp.name, 'alert.txt')
        self.cfg = {
            'heartbeat_file': self.hb,
            'heartbeat_watchdog': {
                'alert_after_sec': 300,
                'alert_cooldown_sec': 0,
                'alert_marker_file': self.marker,
            },
        }

    def _write_hb(self, content: str, *, age_sec: float = 0.0) -> None:
        with open(self.hb, 'w', encoding='utf-8') as f:
            f.write(content)
        if age_sec > 0:
            ts = time.time() - age_sec
            os.utime(self.hb, (ts, ts))

    def test_stopped_status_skips_alert(self):
        from scripts.check_heartbeat_watchdog import run_check

        self._write_hb('status=STOPPED\nreason=SIGTERM\n', age_sec=9999.0)
        with patch('merged_config.load_merged_config', return_value=self.cfg):
            self.assertEqual(run_check(), 0)

    def test_fresh_heartbeat_ok(self):
        from scripts.check_heartbeat_watchdog import run_check

        self._write_hb('status=RUNNING\npid=1\n', age_sec=10.0)
        with patch('merged_config.load_merged_config', return_value=self.cfg):
            self.assertEqual(run_check(), 0)

    def test_stale_heartbeat_alerts(self):
        from scripts.check_heartbeat_watchdog import run_check

        self._write_hb('status=RUNNING\npid=1\n', age_sec=400.0)
        with patch('merged_config.load_merged_config', return_value=self.cfg), \
             patch('scripts.check_heartbeat_watchdog._send_alert', return_value=True) as m:
            self.assertEqual(run_check(), 1)
        m.assert_called_once()

    def test_missing_file_alerts(self):
        from scripts.check_heartbeat_watchdog import run_check

        with patch('merged_config.load_merged_config', return_value=self.cfg), \
             patch('scripts.check_heartbeat_watchdog._send_alert', return_value=True) as m:
            self.assertEqual(run_check(), 1)
        m.assert_called_once()


if __name__ == '__main__':
    unittest.main()
