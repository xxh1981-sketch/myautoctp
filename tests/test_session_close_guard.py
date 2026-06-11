"""session_close_calendar / session_close_guard 单元测试。"""

import os
import sys
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from session_close_calendar import (
    get_session_phase,
    is_trading_time_at,
    seconds_to_segment_end,
)
import session_close_guard as scg


class TestSessionCloseCalendar(unittest.TestCase):

    def test_m_day_afternoon_t10(self):
        # Thursday 2026-06-11 14:55 -> 5 min to 15:00 close
        now = datetime(2026, 6, 11, 14, 55, 0)
        cfg = {'session_close_guard': {'enabled': True}}
        self.assertTrue(is_trading_time_at('m', now, cfg))
        sec = seconds_to_segment_end('m', now, cfg)
        self.assertIsNotNone(sec)
        self.assertLessEqual(sec, 310)
        self.assertGreater(sec, 240)
        self.assertEqual(get_session_phase('m', cfg, now), 't10')

    def test_m_day_normal(self):
        now = datetime(2026, 6, 11, 10, 0, 0)
        cfg = {'session_close_guard': {'enabled': True}}
        self.assertEqual(get_session_phase('m', cfg, now), 'normal')

    def test_m_night_t1(self):
        # m night ends 23:00; 22:59:30 -> ~30s left
        now = datetime(2026, 6, 11, 22, 59, 30)
        cfg = {'session_close_guard': {'enabled': True}}
        self.assertEqual(get_session_phase('m', cfg, now), 't1')

    def test_off_after_close(self):
        now = datetime(2026, 6, 11, 23, 24, 0)
        cfg = {'session_close_guard': {'enabled': True}}
        self.assertEqual(get_session_phase('m', cfg, now), 'off')

    def test_guard_disabled_always_normal(self):
        now = datetime(2026, 6, 11, 22, 59, 0)
        cfg = {'session_close_guard': {'enabled': False}}
        self.assertEqual(get_session_phase('m', cfg, now), 'normal')


class TestSessionCloseGuardHelpers(unittest.TestCase):

    def test_strangle_incomplete_open(self):
        ledger = MagicMock()
        ledger.list_unmatched_legs.return_value = [
            {'symbol': 'm', 'kind': 'awaiting_phase2'},
        ]
        self.assertTrue(scg.strangle_has_incomplete_open(ledger, 'm'))
        self.assertFalse(scg.strangle_has_incomplete_open(ledger, 'i'))

    def test_spread_combo_in_progress_executor(self):
        conn = MagicMock()
        conn._active_executor = MagicMock(symbol='m')
        conn._closing_lock = MagicMock()
        conn._closing_lock.locked.return_value = False
        self.assertTrue(scg.spread_combo_in_progress(conn, 'm'))

    def test_prune_stale_pending(self):
        conn = MagicMock()
        conn.lock = MagicMock()
        conn.lock.__enter__ = MagicMock(return_value=None)
        conn.lock.__exit__ = MagicMock(return_value=False)
        order = MagicMock(instrument_id='m2609-C-3400')
        order.order_ref = 500344
        conn.pending_orders = {500344: order}
        conn.order_traded = {500344: False}
        conn.query_orders_sync.return_value = []
        removed = scg.prune_stale_pending_orders(conn, 'm', logger=None)
        self.assertEqual(removed, 1)
        self.assertEqual(conn.pending_orders, {})

    def test_t1_sweep_dedupes(self):
        conn = MagicMock()
        conn.td_logined = True
        conn.config = {'CANCEL_ALL_TIMEOUT': 1}
        conn._runtime_state = {}
        order = MagicMock(instrument_id='m2609-C-3400')
        order.order_ref = 500344
        conn.pending_orders = {500344: order}
        conn.lock = MagicMock()
        conn.lock.__enter__ = MagicMock(return_value=None)
        conn.lock.__exit__ = MagicMock(return_value=False)
        conn.query_orders_sync.return_value = [
            {'order_ref': 500344, 'status': '3'},
        ]
        conn.cancel_all_pending_orders.return_value = 1
        logger = MagicMock()
        cfg = {
            'session_close_guard': {
                'enabled': True,
                'hard_stop_before_close_sec': 60,
                'no_new_group_before_close_sec': 600,
            },
        }
        tradeinfo = [{'future': 'm'}]
        with patch('session_close_guard.is_trading_time_at', return_value=True), \
             patch('session_close_guard.get_session_phase', return_value='t1'), \
             patch('session_close_guard.segment_end_id', return_value='20260611-2300'):
            scg.maybe_run_pre_close_cancel_sweep(conn, tradeinfo, cfg, logger)
            scg.maybe_run_pre_close_cancel_sweep(conn, tradeinfo, cfg, logger)
        conn.cancel_all_pending_orders.assert_called_once()


if __name__ == '__main__':
    unittest.main()
