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

    def test_m_morning_break_off(self):
        now = datetime(2026, 6, 11, 10, 20, 0)
        cfg = {'session_close_guard': {'enabled': True}}
        self.assertFalse(is_trading_time_at('m', now, cfg))
        self.assertEqual(get_session_phase('m', cfg, now), 'off')

    def test_m_before_morning_break_not_t10(self):
        now = datetime(2026, 6, 11, 10, 6, 0)
        cfg = {
            'session_close_guard': {
                'enabled': True,
                'no_new_group_before_close_sec': 600,
                'hard_stop_before_close_sec': 60,
            },
        }
        self.assertTrue(is_trading_time_at('m', now, cfg))
        self.assertEqual(get_session_phase('m', cfg, now), 'normal')

    def test_io_morning_break_still_trading(self):
        now = datetime(2026, 6, 11, 10, 20, 0)
        cfg = {'session_close_guard': {'enabled': True}}
        self.assertTrue(is_trading_time_at('io', now, cfg))
        self.assertEqual(get_session_phase('io', cfg, now), 'normal')

    def test_tf_morning_break_still_trading(self):
        now = datetime(2026, 6, 11, 10, 20, 0)
        cfg = {'session_close_guard': {'enabled': True}}
        self.assertTrue(is_trading_time_at('tf', now, cfg))
        self.assertEqual(get_session_phase('tf', cfg, now), 'normal')

    def test_m_include_morning_break_enables_t10_at_1006(self):
        now = datetime(2026, 6, 11, 10, 6, 0)
        cfg = {
            'session_close_guard': {
                'enabled': True,
                'include_morning_break': True,
                'no_new_group_before_close_sec': 600,
                'hard_stop_before_close_sec': 60,
            },
        }
        self.assertEqual(get_session_phase('m', cfg, now), 't10')

    def test_m_night_t1(self):
        # m night ends 23:00; 22:59:30 -> ~30s left
        now = datetime(2026, 6, 11, 22, 59, 30)
        cfg = {'session_close_guard': {'enabled': True}}
        self.assertEqual(get_session_phase('m', cfg, now), 't1')

    def test_off_after_close(self):
        now = datetime(2026, 6, 11, 23, 24, 0)
        cfg = {'session_close_guard': {'enabled': True}}
        self.assertEqual(get_session_phase('m', cfg, now), 'off')

    def test_day_close_subminute_boundary(self):
        """15:00:10 must not be trading; phase/send guard must not dead-zone."""
        cfg = {
            'session_close_guard': {
                'enabled': True,
                'hard_stop_before_close_sec': 60,
                'no_new_group_before_close_sec': 600,
            },
        }
        t1 = datetime(2026, 6, 15, 14, 59, 40)
        past = datetime(2026, 6, 15, 15, 0, 10)
        self.assertEqual(get_session_phase('m', cfg, t1), 't1')
        self.assertFalse(is_trading_time_at('m', past, cfg))
        self.assertEqual(get_session_phase('m', cfg, past), 'off')
        self.assertIsNone(seconds_to_segment_end('m', past, cfg))

    def test_exact_segment_end_still_trading(self):
        cfg = {'session_close_guard': {'enabled': True}}
        now = datetime(2026, 6, 15, 15, 0, 0)
        self.assertTrue(is_trading_time_at('m', now, cfg))
        self.assertEqual(get_session_phase('m', cfg, now), 't1')

    def test_guard_disabled_always_normal(self):
        now = datetime(2026, 6, 11, 22, 59, 0)
        cfg = {'session_close_guard': {'enabled': False}}
        self.assertEqual(get_session_phase('m', cfg, now), 'normal')

    def test_disabled_install_does_not_mark_installed(self):
        scg._INSTALLED = False
        cfg = {'session_close_guard': {'enabled': False}}
        self.assertTrue(scg.install_session_close_guard(cfg))
        self.assertFalse(scg.is_installed())


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
        conn._runtime_state = {}
        conn._closing_lock = MagicMock()
        conn._closing_lock.locked.return_value = False
        self.assertTrue(scg.spread_combo_in_progress(conn, 'm'))
        self.assertFalse(scg.spread_combo_in_progress(conn, 'rm'))

    def test_spread_combo_in_progress_per_symbol_lock(self):
        conn = MagicMock()
        conn._active_executor = None
        conn._runtime_state = {}
        conn._closing_lock_symbol = 'm'
        conn._closing_lock = MagicMock()
        conn._closing_lock.locked.return_value = True
        self.assertTrue(scg.spread_combo_in_progress(conn, 'm'))
        self.assertFalse(scg.spread_combo_in_progress(conn, 'rm'))

    def test_should_block_send_t1_and_after_hours(self):
        conn = MagicMock()
        conn._runtime_state = {}
        cfg = {
            'session_close_guard': {
                'enabled': True,
                'hard_stop_before_close_sec': 60,
            },
        }
        t1 = datetime(2026, 6, 15, 14, 59, 40)
        past = datetime(2026, 6, 15, 15, 0, 10)
        night_past = datetime(2026, 7, 6, 23, 22, 21)
        self.assertTrue(scg.should_block_send(conn, 'm', cfg, t1))
        self.assertTrue(scg.should_block_send(conn, 'm', cfg, past))
        self.assertTrue(scg.should_block_send(conn, 'm', cfg, night_past))

    def test_should_skip_strangle_rebalance_off_hours(self):
        conn = MagicMock()
        ledger = MagicMock()
        ledger.list_unmatched_legs.return_value = [
            {'symbol': 'm', 'month': '2701', 'kind': 'inferred_single'},
        ]
        cfg = {'session_close_guard': {'enabled': True}}
        with patch('session_close_guard.get_session_phase', return_value='off'):
            skip, reason = scg.should_skip_strangle_rebalance(
                conn, cfg, ledger=ledger,
                tradeinfo=[{'future': 'm', 'month': '2701'}],
            )
        self.assertTrue(skip)
        self.assertIn('非交易时段', reason)

    def test_should_block_send_after_t1_abort_flag(self):
        conn = MagicMock()
        conn._runtime_state = {'_session_t1_abort_at:m:20260615-1500': 1.0}
        cfg = {'session_close_guard': {'enabled': True}}
        mid = datetime(2026, 6, 15, 14, 59, 20)
        with patch('session_close_guard.get_session_phase', return_value='normal'), \
             patch('session_close_guard.is_trading_time_at', return_value=True), \
             patch('session_close_guard.segment_end_id', return_value='20260615-1500'):
            self.assertTrue(scg.should_block_send(conn, 'm', cfg, mid))

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
        self.assertIn('_session_t1_abort_at:m:20260611-2300', conn._runtime_state)


class TestSessionCloseGuardIntegration(unittest.TestCase):
    """Install 链 + guarded _send_and_wait / process_close 行为（mock 底层 CTP）。"""

    def setUp(self):
        import autotrade_stubs as ats

        ats.ensure_autotrade_stubs([
            'auto_closer_executor',
            'auto_order_manager',
            'auto_closer',
            'auto_processor',
            'auto_connection',
        ])
        import auto_closer_executor as ace
        import auto_order_manager as aom
        import auto_closer as ac

        self._ace = ace
        self._aom = aom
        self._ac = ac
        self._saved_installed = scg._INSTALLED
        self._orig_send = ace._send_and_wait
        self._orig_close_leg = ace._close_single_leg
        self._orig_execute = ace.execute_close_orders_with_limit
        self._orig_om_send = aom.OrderManager.send_order
        self._orig_process_close = ac.process_close
        scg._INSTALLED = False

    def tearDown(self):
        self._ace._send_and_wait = self._orig_send
        self._ace._close_single_leg = self._orig_close_leg
        self._ace.execute_close_orders_with_limit = self._orig_execute
        self._aom.OrderManager.send_order = self._orig_om_send
        self._ac.process_close = self._orig_process_close
        scg._INSTALLED = self._saved_installed

    def _cfg(self):
        return {
            'session_close_guard': {
                'enabled': True,
                'hard_stop_before_close_sec': 60,
                'no_new_group_before_close_sec': 600,
            },
        }

    def _install(self):
        self.assertTrue(scg.install_session_close_guard(self._cfg()))
        self.assertTrue(scg.is_installed())

    def test_guarded_close_single_leg_blocks_off_hours(self):
        called = []

        def orig_leg(conn, contract, *a, **kw):
            called.append(contract)
            return True, 1, 1.0

        self._ace._close_single_leg = orig_leg
        self._install()

        conn = MagicMock()
        conn._runtime_state = {}
        logger = MagicMock()
        with patch('session_close_guard.is_trading_time_at', return_value=False), \
             patch('session_close_guard.get_session_phase', return_value='off'):
            filled, traded, px = self._ace._close_single_leg(
                conn, 'm2701-P-2900', '0', 1, 26.0, 1.0,
                self._cfg(), logger, 'm',
            )
        self.assertEqual((filled, traded, px), (False, 0, 0.0))
        self.assertEqual(called, [])

    def test_guarded_send_and_wait_blocks_when_should_block(self):
        sent = []

        def orig_send(conn, instrument, direction, volume, price, offset,
                      timeout, config, logger, symbol, **kwargs):
            sent.append(instrument)
            return True, volume, price

        self._ace._send_and_wait = orig_send
        self._install()

        conn = MagicMock()
        conn._runtime_state = {}
        cfg = self._cfg()
        logger = MagicMock()

        with patch('session_close_guard.should_block_send', return_value=True):
            result = self._ace._send_and_wait(
                conn, 'm2609-C-3000', '0', 2, 41.0, '1',
                120, cfg, logger, 'm',
            )

        self.assertEqual(result, (False, 0, 0.0))
        self.assertEqual(sent, [])
        logger.info.assert_called()

    def test_guarded_send_and_wait_delegates_when_allowed(self):
        sent = []

        def orig_send(conn, instrument, direction, volume, price, offset,
                      timeout, config, logger, symbol, **kwargs):
            sent.append(instrument)
            return True, volume, price

        self._ace._send_and_wait = orig_send
        self._install()

        conn = MagicMock()
        conn._runtime_state = {}
        cfg = self._cfg()

        with patch('session_close_guard.should_block_send', return_value=False):
            result = self._ace._send_and_wait(
                conn, 'm2609-C-3000', '0', 2, 41.0, '1',
                120, cfg, MagicMock(), 'm',
            )

        self.assertEqual(result, (True, 2, 41.0))
        self.assertEqual(sent, ['m2609-C-3000'])

    def test_t1_sweep_abort_flag_blocks_subsequent_close_send(self):
        """模拟 6/15：T-1 撤单清扫后，进行中平仓重试不得再发单。"""
        sent = []

        def orig_send(conn, instrument, direction, volume, price, offset,
                      timeout, config, logger, symbol, **kwargs):
            sent.append(instrument)
            return True, volume, price

        self._ace._send_and_wait = orig_send
        self._install()

        conn = MagicMock()
        conn.td_logined = True
        conn.config = self._cfg()
        conn._runtime_state = {}
        conn.pending_orders = {}
        conn.lock = MagicMock()
        conn.lock.__enter__ = MagicMock(return_value=None)
        conn.lock.__exit__ = MagicMock(return_value=False)
        conn.query_orders_sync.return_value = []
        conn.cancel_all_pending_orders.return_value = 1
        logger = MagicMock()
        cfg = self._cfg()
        seg_id = '20260615-1500'

        with patch('session_close_guard.is_trading_time_at', return_value=True), \
             patch('session_close_guard.get_session_phase', return_value='t1'), \
             patch('session_close_guard.segment_end_id', return_value=seg_id):
            scg.maybe_run_pre_close_cancel_sweep(
                conn, [{'future': 'm'}], cfg, logger,
            )

        self.assertIn(f'_session_t1_abort_at:m:{seg_id}', conn._runtime_state)

        with patch('session_close_guard.get_session_phase', return_value='normal'), \
             patch('session_close_guard.is_trading_time_at', return_value=True), \
             patch('session_close_guard.segment_end_id', return_value=seg_id):
            result = self._ace._send_and_wait(
                conn, 'm2609-C-3000', '0', 2, 41.0, '1',
                120, cfg, logger, 'm',
            )

        self.assertEqual(result, (False, 0, 0.0))
        self.assertEqual(sent, [])

    def test_process_close_t10_blocks_new_symbol_while_other_closing(self):
        """全局 _closing_lock 持有中，T-10 仍应拒绝其它品种新平仓。"""
        close_calls = []

        def orig_close(conn, item, vix, config, logger, positions=None):
            close_calls.append(item['future'])
            return False

        self._ac.process_close = orig_close
        self._install()

        conn = MagicMock()
        conn._runtime_state = {}
        conn._active_executor = None
        conn._closing_lock = MagicMock()
        conn._closing_lock.locked.return_value = True
        conn._closing_lock_symbol = 'm'

        import auto_closer

        with patch('session_close_guard.get_session_phase', return_value='t10'):
            blocked = auto_closer.process_close(
                conn, {'future': 'rm'}, 19.0, self._cfg(), MagicMock(),
            )

        self.assertFalse(blocked)
        self.assertEqual(close_calls, [])

    def test_execute_close_marks_active_only_for_same_symbol(self):
        seen = []

        def orig_execute(conn, plan, symbol, month, min_tick, config, logger,
                         urgency='urgent'):
            seen.append(scg.spread_combo_in_progress(conn, 'm'))
            seen.append(scg.spread_combo_in_progress(conn, 'rm'))
            return True, 1

        self._ace.execute_close_orders_with_limit = orig_execute
        self._install()

        conn = MagicMock()
        conn._runtime_state = {}

        self._ace.execute_close_orders_with_limit(
            conn, [], 'm', '2609', 1.0, self._cfg(), MagicMock(),
        )

        self.assertEqual(seen, [True, False])
        self.assertNotIn('_spread_close_active:m', conn._runtime_state)

    def test_order_manager_send_order_guard_blocks(self):
        sent = []

        def orig_send(self, instrument, *args, **kwargs):
            sent.append(instrument)
            return 99, '99'

        self._aom.OrderManager.send_order = orig_send
        self._install()

        conn = MagicMock()
        conn._runtime_state = {}
        conn.config = self._cfg()
        mgr = self._aom.OrderManager(conn, self._cfg(), MagicMock())

        with patch('session_close_guard.should_block_send', return_value=True):
            ref, ref_str = mgr.send_order('m2609-C-3000', '0', 1, 41.0)

        self.assertIsNone(ref)
        self.assertIsNone(ref_str)
        self.assertEqual(sent, [])


if __name__ == '__main__':
    unittest.main()
