"""merged_main_loop 单元测试（重连隔离 / 看门狗）"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ctp_bootstrap  # noqa: F401


class FakeLogger:
    def __init__(self):
        self.messages = []

    def isEnabledFor(self, level):
        return True

    def info(self, msg, *a, **kw):
        self.messages.append(('INFO', msg))

    def warning(self, msg, *a, **kw):
        self.messages.append(('WARNING', msg))

    def error(self, msg, *a, **kw):
        self.messages.append(('ERROR', msg))

    def debug(self, msg, *a, **kw):
        self.messages.append(('DEBUG', msg))

    def log(self, level, msg, *a, **kw):
        self.messages.append(('LOG', msg))


def _make_conn():
    conn = MagicMock()
    conn.config = {
        'loop_interval': 0.01,
        'daily_trade_limit': 5,
        'CANCEL_ALL_TIMEOUT': 2,
        'health_alert_cooldown': 300,
        'scheduled_full_recovery_enabled': False,
        'scheduled_session_pause_enabled': False,
    }
    conn.get_filled_open_order_count = MagicMock(return_value=0)
    conn.cancel_all_pending_orders = MagicMock(return_value=0)
    conn._runtime_state = {}
    conn._td_disconnect_notified = False
    conn._md_disconnect_notified = False
    conn._reconnect_quarantine = False
    conn.td_logined = True
    conn.md_logined = True
    conn._executor_lock = __import__('threading').Lock()
    conn._active_executor = None
    return conn


def _minimal_args(conn, logger):
    return dict(
        conn=conn,
        spread_tradeinfo=[],
        strangle_tradeinfo=[],
        combined_tradeinfo=[],
        vix_engine=MagicMock(),
        config=conn.config,
        logger=logger,
        ledger=MagicMock(),
    )


class TestMergedMainLoopReconnect(unittest.TestCase):

    @patch('auto_reconnect_recovery.check_quarantine_watchdog')
    @patch('auto_scheduled_pause.is_connection_suspended', return_value=False)
    def test_quarantine_invokes_watchdog(self, mock_suspended, mock_watchdog):
        conn = _make_conn()
        conn._reconnect_quarantine = True
        logger = FakeLogger()

        with patch('auto_feishu_command.stop_command_receiver'), \
             patch('auto_feishu_command.start_command_receiver'), \
             patch('auto_scheduled_pause.sync_connection_suspend_state'), \
             patch('auto_circuit_breaker.CircuitBreaker'), \
             patch('straggle_execution.StrangleExecutor'), \
             patch('auto_health_check.HealthChecker') as mock_hc:
            mock_hc.return_value.check_now.return_value = {'healthy': True}
            from merged_main_loop import run_merged_main_loop
            with patch('time.sleep', side_effect=KeyboardInterrupt):
                run_merged_main_loop(**_minimal_args(conn, logger))

        mock_watchdog.assert_called_once_with(conn, conn.config, logger)
        quarantine_logs = [m for lvl, m in logger.messages if '隔离期' in m]
        self.assertTrue(quarantine_logs)

    @patch('auto_reconnect_recovery.check_quarantine_watchdog')
    @patch('auto_scheduled_pause.is_connection_suspended', return_value=True)
    def test_watchdog_skipped_during_session_pause(self, mock_suspended, mock_watchdog):
        conn = _make_conn()
        conn._reconnect_quarantine = True
        logger = FakeLogger()

        with patch('auto_feishu_command.stop_command_receiver'), \
             patch('auto_feishu_command.start_command_receiver'), \
             patch('auto_scheduled_pause.sync_connection_suspend_state'), \
             patch('auto_circuit_breaker.CircuitBreaker'), \
             patch('straggle_execution.StrangleExecutor'), \
             patch('auto_health_check.HealthChecker') as mock_hc:
            mock_hc.return_value.check_now.return_value = {'healthy': True}
            from merged_main_loop import run_merged_main_loop
            with patch('time.sleep', side_effect=KeyboardInterrupt):
                run_merged_main_loop(**_minimal_args(conn, logger))

        mock_watchdog.assert_not_called()


    @patch('auto_reconnect_recovery.check_quarantine_watchdog')
    @patch('auto_scheduled_pause.is_connection_suspended', return_value=False)
    def test_not_logged_in_logs_without_watchdog(self, _mock_suspended, mock_watchdog):
        conn = _make_conn()
        conn.td_logined = False
        conn.md_logined = True
        logger = FakeLogger()

        with patch('auto_feishu_command.stop_command_receiver'), \
             patch('auto_feishu_command.start_command_receiver'), \
             patch('auto_scheduled_pause.sync_connection_suspend_state'), \
             patch('auto_circuit_breaker.CircuitBreaker'), \
             patch('straggle_execution.StrangleExecutor'), \
             patch('auto_health_check.HealthChecker') as mock_hc:
            mock_hc.return_value.check_now.return_value = {'healthy': True}
            from merged_main_loop import run_merged_main_loop
            with patch('time.sleep', side_effect=KeyboardInterrupt):
                run_merged_main_loop(**_minimal_args(conn, logger))

        mock_watchdog.assert_not_called()
        login_logs = [m for lvl, m in logger.messages if '未全部登录' in m]
        self.assertTrue(login_logs)


if __name__ == '__main__':
    unittest.main()
