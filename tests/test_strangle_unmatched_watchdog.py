"""strangle_unmatched_watchdog unit tests."""

import os
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ctp_bootstrap  # noqa: F401

from strangle_unmatched_watchdog import (
    STATE_FIRST_SEEN,
    STATE_LAST_ALERTED,
    check_unmatched_health,
)


class FakeLedger:
    def __init__(self, legs):
        self._legs = list(legs)

    def list_unmatched_legs(self):
        return list(self._legs)


class FakeConn:
    def __init__(self):
        self._runtime_state = {}


def _leg(retry, sym='sa', month='609', kind='close_chp_pending', inst='SA609C2400'):
    return {
        'symbol': sym,
        'month': month,
        'kind': kind,
        'b_retry_count': retry,
        'leg': {'inst': inst, 'label': 'Call'},
    }


class TestUnmatchedWatchdog(unittest.TestCase):

    def test_no_alert_when_no_stuck(self):
        conn = FakeConn()
        led = FakeLedger([_leg(retry=3)])
        cfg = {'strangle': {'phase2_max_retries': 10}}
        with patch('auto_feishu.send_feishu_message') as mock_send:
            check_unmatched_health(conn, led, cfg, None)
        mock_send.assert_not_called()
        self.assertEqual(conn._runtime_state[STATE_FIRST_SEEN], {})

    def test_first_observation_records_timestamp_but_no_alert(self):
        conn = FakeConn()
        led = FakeLedger([_leg(retry=10)])
        cfg = {'strangle': {'phase2_max_retries': 10}}
        with patch('auto_feishu.send_feishu_message') as mock_send:
            check_unmatched_health(conn, led, cfg, None)
        mock_send.assert_not_called()
        self.assertEqual(len(conn._runtime_state[STATE_FIRST_SEEN]), 1)

    def test_alert_after_threshold_age(self):
        conn = FakeConn()
        led = FakeLedger([_leg(retry=10)])
        cfg = {
            'strangle': {
                'phase2_max_retries': 10,
                'unmatched_stuck_alert_age_sec': 1,
            }
        }
        with patch('auto_feishu.send_feishu_message') as mock_send:
            check_unmatched_health(conn, led, cfg, None)
            mock_send.assert_not_called()
            time.sleep(1.05)
            check_unmatched_health(conn, led, cfg, None)
            self.assertEqual(mock_send.call_count, 1)

    def test_alert_cooldown_blocks_repeats(self):
        conn = FakeConn()
        led = FakeLedger([_leg(retry=10)])
        cfg = {
            'strangle': {
                'phase2_max_retries': 10,
                'unmatched_stuck_alert_age_sec': 0,
                'unmatched_stuck_alert_cooldown_sec': 60,
            }
        }
        with patch('auto_feishu.send_feishu_message') as mock_send:
            check_unmatched_health(conn, led, cfg, None)
            check_unmatched_health(conn, led, cfg, None)
            check_unmatched_health(conn, led, cfg, None)
        self.assertEqual(mock_send.call_count, 1)

    def test_state_cleared_when_leg_disappears(self):
        conn = FakeConn()
        led_stuck = FakeLedger([_leg(retry=10)])
        cfg = {'strangle': {'phase2_max_retries': 10}}
        with patch('auto_feishu.send_feishu_message'):
            check_unmatched_health(conn, led_stuck, cfg, None)
        self.assertEqual(len(conn._runtime_state[STATE_FIRST_SEEN]), 1)
        led_empty = FakeLedger([])
        with patch('auto_feishu.send_feishu_message'):
            check_unmatched_health(conn, led_empty, cfg, None)
        self.assertEqual(conn._runtime_state[STATE_FIRST_SEEN], {})
        self.assertEqual(conn._runtime_state[STATE_LAST_ALERTED], {})


if __name__ == '__main__':
    unittest.main()
