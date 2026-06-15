"""shutdown_cancel / symbol_scan_timeout 退出撤单竞态修复。"""

from __future__ import annotations

import threading
import unittest
from concurrent.futures import Future
from unittest.mock import MagicMock, patch

import shutdown_cancel as sc
import symbol_scan_timeout as sst


class TestShutdownSendGuard(unittest.TestCase):

    def setUp(self):
        sc._SEND_GUARD_INSTALLED = False
        try:
            import auto_order_manager as aom
            if hasattr(aom.OrderManager.send_order, '_shutdown_cancel_wrapped'):
                # restore if another test installed
                pass
        except ImportError:
            self.skipTest('autotrade not available')

    def test_blocks_send_when_shutdown_flag_set(self):
        import auto_order_manager as aom

        calls = {'n': 0}

        def original(self, instrument, *args, **kwargs):
            calls['n'] += 1
            return 1, '1'

        aom.OrderManager.send_order = original
        sc._SEND_GUARD_INSTALLED = False
        self.assertTrue(sc.install_shutdown_send_guard())

        conn = MagicMock()
        conn._runtime_state = {sc._SHUTDOWN_FLAG: True}
        mgr = aom.OrderManager(conn, {}, MagicMock())

        ref, _ = mgr.send_order('m2609-C-3400', '0', 1, 7.5)
        self.assertIsNone(ref)
        self.assertEqual(calls['n'], 0)


class TestCancelPendingOnShutdown(unittest.TestCase):

    def setUp(self):
        sc._CANCEL_STARTED = False

    def tearDown(self):
        sc._CANCEL_STARTED = False

    def test_dual_pass_cancel(self):
        conn = MagicMock()
        conn.td_logined = True
        conn.td_api = MagicMock()
        conn._runtime_state = {}
        conn._executor_lock = threading.Lock()
        conn._active_executor = None
        conn.config = {
            'CANCEL_ALL_TIMEOUT': 2,
            'shutdown_cancel_passes': 2,
            'shutdown_cancel_pass_pause_sec': 0,
            'shutdown_cancel_confirm_timeout': 5,
            'shutdown_scan_drain_sec': 0,
        }
        conn.cancel_all_pending_orders = MagicMock(side_effect=[1, 0])
        logger = MagicMock()

        total = sc.cancel_pending_on_shutdown(conn, conn.config, logger)
        self.assertEqual(total, 1)
        self.assertEqual(conn.cancel_all_pending_orders.call_count, 2)
        self.assertTrue(conn._runtime_state.get(sc._SHUTDOWN_FLAG))

    def test_second_cancel_is_skipped(self):
        conn = MagicMock()
        conn.td_logined = True
        conn.td_api = MagicMock()
        conn._runtime_state = {}
        conn._executor_lock = threading.Lock()
        conn._active_executor = None
        conn.config = {
            'CANCEL_ALL_TIMEOUT': 2,
            'shutdown_cancel_passes': 1,
            'shutdown_cancel_pass_pause_sec': 0,
            'shutdown_scan_drain_sec': 0,
        }
        conn.cancel_all_pending_orders = MagicMock(return_value=0)
        logger = MagicMock()

        self.assertEqual(sc.cancel_pending_on_shutdown(conn, conn.config, logger), 0)
        self.assertEqual(sc.cancel_pending_on_shutdown(conn, conn.config, logger), 0)
        conn.cancel_all_pending_orders.assert_called_once()

    def test_stops_active_executor(self):
        conn = MagicMock()
        conn.td_logined = True
        conn.td_api = MagicMock()
        conn._runtime_state = {}
        conn._executor_lock = threading.Lock()
        ex = MagicMock()
        conn._active_executor = ex
        conn.config = {
            'shutdown_scan_drain_sec': 0,
            'shutdown_cancel_passes': 1,
            'shutdown_cancel_pass_pause_sec': 0,
        }
        conn.cancel_all_pending_orders = MagicMock(return_value=0)

        sc.cancel_pending_on_shutdown(conn, conn.config, MagicMock())
        ex.stop_all_threads.set.assert_called_once()
        ex.cleanup.assert_called_once()
        self.assertIsNone(conn._active_executor)


    def test_cancel_exception_restores_confirm_timeout(self):
        conn = MagicMock()
        conn.td_logined = True
        conn.td_api = MagicMock()
        conn._runtime_state = {}
        conn._executor_lock = threading.Lock()
        conn._active_executor = None
        conn.config = {
            'CANCEL_ALL_TIMEOUT': 2,
            'shutdown_cancel_passes': 2,
            'shutdown_cancel_pass_pause_sec': 0,
            'shutdown_cancel_confirm_timeout': 5,
            'shutdown_scan_drain_sec': 0,
        }
        conn.cancel_all_pending_orders = MagicMock(side_effect=RuntimeError('boom'))

        total = sc.cancel_pending_on_shutdown(conn, conn.config, MagicMock())

        self.assertEqual(total, 0)
        self.assertEqual(conn.cancel_all_pending_orders.call_count, 2)
        self.assertNotIn('CANCEL_CONFIRM_TIMEOUT', conn.config)

    @patch('shutdown_cancel._ensure_td_for_shutdown', return_value=False)
    def test_no_td_connection_skips_cancel(self, mock_td):
        conn = MagicMock()
        conn.td_logined = False
        conn.td_api = None
        conn._runtime_state = {}
        conn._executor_lock = threading.Lock()
        conn._active_executor = None
        conn.config = {
            'shutdown_cancel_passes': 2,
            'shutdown_cancel_pass_pause_sec': 0,
            'shutdown_cancel_login_timeout': 0,
            'shutdown_scan_drain_sec': 0,
        }
        conn.cancel_all_pending_orders = MagicMock(return_value=0)

        total = sc.cancel_pending_on_shutdown(conn, conn.config, MagicMock())

        self.assertEqual(total, 0)
        conn.cancel_all_pending_orders.assert_not_called()
        mock_td.assert_called_once()

    @patch('shutdown_cancel._ensure_td_for_shutdown', return_value=False)
    def test_td_failure_allows_retry(self, mock_td):
        conn = MagicMock()
        conn.td_logined = False
        conn.td_api = None
        conn._runtime_state = {}
        conn._executor_lock = threading.Lock()
        conn._active_executor = None
        conn.config = {
            'shutdown_cancel_passes': 1,
            'shutdown_cancel_pass_pause_sec': 0,
            'shutdown_scan_drain_sec': 0,
        }
        conn.cancel_all_pending_orders = MagicMock(return_value=1)

        with patch('shutdown_cancel._ensure_td_for_shutdown', side_effect=[False, True]):
            self.assertEqual(sc.cancel_pending_on_shutdown(conn, conn.config, MagicMock()), 0)
            self.assertEqual(sc.cancel_pending_on_shutdown(conn, conn.config, MagicMock()), 1)

        conn.cancel_all_pending_orders.assert_called_once()

    def test_prepare_shutdown_creates_runtime_state(self):
        conn = type('Conn', (), {})()
        conn._executor_lock = threading.Lock()
        conn._active_executor = None
        sc.prepare_shutdown(conn, {'shutdown_scan_drain_sec': 0}, MagicMock())
        self.assertIsInstance(conn._runtime_state, dict)
        self.assertTrue(conn._runtime_state.get(sc._SHUTDOWN_FLAG))

    def test_bad_shutdown_config_still_cancels(self):
        conn = MagicMock()
        conn.td_logined = True
        conn.td_api = MagicMock()
        conn._runtime_state = {}
        conn._executor_lock = threading.Lock()
        conn._active_executor = None
        conn.config = {
            'shutdown_cancel_passes': 'bad',
            'shutdown_cancel_pass_pause_sec': 'bad',
            'CANCEL_ALL_TIMEOUT': 'bad',
            'shutdown_cancel_confirm_timeout': 'bad',
            'shutdown_scan_drain_sec': 'bad',
        }
        conn.cancel_all_pending_orders = MagicMock(return_value=1)
        total = sc.cancel_pending_on_shutdown(conn, conn.config, MagicMock())
        self.assertEqual(total, 2)
        self.assertEqual(conn.cancel_all_pending_orders.call_count, 2)


class TestShutdownFastFailGuards(unittest.TestCase):

    def setUp(self):
        sc._FAST_FAIL_GUARD_INSTALLED = False

    def test_query_wait_skips_on_shutdown_flag(self):
        try:
            import auto_query_service as aqs
        except ImportError:
            self.skipTest('autotrade not available')
        sc._FAST_FAIL_GUARD_INSTALLED = False
        self.assertTrue(sc._install_query_wait_guard())
        conn = MagicMock()
        conn.td_logined = False
        conn.td_api = None
        conn._runtime_state = {sc._SHUTDOWN_FLAG: True}
        conn._active_executor = None
        logger = MagicMock()
        svc = aqs.QueryService(conn, {}, logger)
        self.assertFalse(svc._wait_for_td_ready(conn, '持仓查询'))
        logger.info.assert_called()

    def test_positions_fallback_skips_on_shutdown_flag(self):
        try:
            import auto_utils as au
        except ImportError:
            self.skipTest('autotrade not available')
        sc._FAST_FAIL_GUARD_INSTALLED = False
        self.assertTrue(sc._install_positions_fallback_guard())
        conn = MagicMock()
        conn._runtime_state = {sc._SHUTDOWN_FLAG: True}
        conn._active_executor = None
        conn.query_positions_sync = MagicMock()
        logger = MagicMock()
        self.assertIsNone(
            au.query_positions_fallback(conn, logger=logger, symbol='m'),
        )
        conn.query_positions_sync.assert_not_called()

    def test_install_fast_fail_idempotent(self):
        try:
            import auto_query_service  # noqa: F401
            import auto_utils  # noqa: F401
        except ImportError:
            self.skipTest('autotrade not available')
        sc._FAST_FAIL_GUARD_INSTALLED = False
        self.assertTrue(sc.install_shutdown_fast_fail_guards())
        self.assertTrue(sc.install_shutdown_fast_fail_guards())


class TestBackgroundScanWait(unittest.TestCase):

    def setUp(self):
        sst._active_scans.clear()

    def tearDown(self):
        sst._active_scans.clear()

    def test_wait_until_future_done(self):
        fut = Future()
        sst._track_future(fut)
        fut.set_result(1)
        sst._untrack_future(fut)
        self.assertEqual(sst.wait_for_background_scans(1.0), 0)

    def test_reports_remaining_after_timeout(self):
        fut = Future()
        sst._track_future(fut)
        try:
            self.assertEqual(sst.wait_for_background_scans(0.3), 1)
        finally:
            sst._untrack_future(fut)


if __name__ == '__main__':
    unittest.main()
