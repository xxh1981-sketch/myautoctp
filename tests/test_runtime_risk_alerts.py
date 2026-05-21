"""Tests for runtime_risk_alerts (notification only)."""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from runtime_risk_alerts import (
    notify_feishu_pause_exposure,
    notify_quarantine_prolonged,
    notify_reconcile_halt_open_unmatched,
    record_margin_check_result,
)


class TestRuntimeRiskAlerts(unittest.TestCase):
    def _conn(self):
        conn = MagicMock()
        conn._runtime_state = {}
        conn._reconnect_quarantine = False
        conn.td_logined = True
        conn.md_logined = True
        return conn

    @patch('runtime_risk_alerts._send_feishu')
    def test_feishu_pause_alerts_on_exposure(self, mock_send):
        conn = self._conn()
        conn._runtime_state['_strangle_reconcile_halt'] = True
        ledger = MagicMock()
        ledger.list_unmatched_legs.return_value = [
            {'symbol': 'sa', 'month': '2608', 'kind': 'awaiting_phase2', 'leg': {}},
        ]
        notify_feishu_pause_exposure(
            conn, ledger, {'risk_alert_cooldown_sec': 0}, None, paused=True,
        )
        self.assertTrue(mock_send.called)
        mock_send.reset_mock()
        notify_feishu_pause_exposure(
            conn, ledger, {'risk_alert_cooldown_sec': 0}, None, paused=True,
        )
        self.assertFalse(mock_send.called)

    @patch('runtime_risk_alerts._send_feishu')
    def test_margin_unknown_streak(self, mock_send):
        conn = self._conn()
        cfg = {
            'margin_unknown_alert_after': 3,
            'risk_alert_cooldown_sec': 0,
        }
        for _ in range(2):
            record_margin_check_result(conn, cfg, None, 'unknown')
        self.assertFalse(mock_send.called)
        record_margin_check_result(conn, cfg, None, 'unknown')
        self.assertTrue(mock_send.called)
        mock_send.reset_mock()
        record_margin_check_result(conn, cfg, None, 'ok')
        record_margin_check_result(conn, cfg, None, 'unknown')
        self.assertFalse(mock_send.called)

    @patch('runtime_risk_alerts._send_feishu')
    def test_quarantine_prolonged(self, mock_send):
        conn = self._conn()
        conn._reconnect_quarantine = True
        import time
        conn._runtime_state['_quarantine_since_ts'] = time.time() - 700
        notify_quarantine_prolonged(
            conn, {'quarantine_alert_after_sec': 600, 'risk_alert_cooldown_sec': 0},
            None,
        )
        self.assertTrue(mock_send.called)

    @patch('runtime_risk_alerts._send_feishu')
    def test_reconcile_halt_open_unmatched(self, mock_send):
        conn = self._conn()
        conn._runtime_state['_strangle_reconcile_halt'] = True
        conn._runtime_state['_strangle_reconcile_issues'] = ['SA gap']
        ledger = MagicMock()
        ledger.list_unmatched_legs.return_value = [
            {'symbol': 'sa', 'month': '2608', 'kind': 'awaiting_phase2', 'leg': {'inst': 'x'}},
        ]
        notify_reconcile_halt_open_unmatched(
            conn, ledger, {'risk_alert_cooldown_sec': 0}, None,
        )
        self.assertTrue(mock_send.called)
        body = mock_send.call_args[0][0]
        self.assertIn('awaiting_phase2', body)


if __name__ == '__main__':
    unittest.main()
