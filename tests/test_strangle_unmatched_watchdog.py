"""strangle_unmatched_watchdog unit tests."""

import os
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ctp_bootstrap  # noqa: F401

import autotrade_stubs

autotrade_stubs.ensure_auto_feishu_stub()

from strangle_unmatched_watchdog import (
    STATE_FIRST_SEEN,
    STATE_LAST_ALERTED,
    STATE_METADATA_LAST_ALERTED,
    _b_max,
    check_unmatched_health,
    validate_unmatched_leg_metadata,
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

    def test_validate_unmatched_leg_metadata(self):
        bad = validate_unmatched_leg_metadata([
            _leg(retry=10),
            {'symbol': 'sa', 'month': '609', 'kind': 'close_chp_pending', 'leg': {}},
            {'symbol': 'sa', 'month': '609', 'kind': 'close_chp_pending', 'leg': {'inst': 'SA609C2400'}, 'b_retry_count': 'bad'},
        ])
        self.assertEqual(len(bad), 2)
        missing_by_index = {item['index']: item['missing'] for item in bad}
        self.assertIn('leg.inst or filled_instrument', missing_by_index[1])
        self.assertIn('b_retry_count', missing_by_index[2])

    def test_metadata_alert_cooldown(self):
        conn = FakeConn()
        led = FakeLedger([{'symbol': 'sa', 'month': '609', 'kind': 'close_chp_pending', 'leg': {}}])
        cfg = {'strangle': {'unmatched_leg_metadata_alert': True, 'unmatched_leg_metadata_alert_cooldown_sec': 60}}
        with patch('auto_feishu.send_feishu_message') as mock_send:
            check_unmatched_health(conn, led, cfg, None)
            check_unmatched_health(conn, led, cfg, None)
        self.assertEqual(mock_send.call_count, 1)
        self.assertIn(STATE_METADATA_LAST_ALERTED, conn._runtime_state)

    def test_metadata_alert_can_be_disabled(self):
        conn = FakeConn()
        led = FakeLedger([{'symbol': 'sa', 'month': '609', 'kind': 'close_chp_pending', 'leg': {}}])
        cfg = {'strangle': {'unmatched_leg_metadata_alert': False}}
        with patch('auto_feishu.send_feishu_message') as mock_send:
            check_unmatched_health(conn, led, cfg, None)
        mock_send.assert_not_called()

    def test_b_max_falls_back_on_bad_config(self):
        self.assertEqual(_b_max({'B_max_retries': 'bad', 'strangle': {}}), 10)
        self.assertEqual(
            _b_max({'strangle': {'phase2_max_retries': 'bad'}}),
            10,
        )


if __name__ == '__main__':
    unittest.main()
