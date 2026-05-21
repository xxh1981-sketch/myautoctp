"""merged_main_loop 单元测试（重连隔离 / 看门狗）"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import autotrade_stubs

autotrade_stubs.ensure_merged_loop_stubs()
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
        quarantine_logs = [m for lvl, m in logger.messages if '隔离' in m]
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

    @patch('auto_scheduled_pause.is_connection_suspended', return_value=True)
    def test_health_check_skipped_during_session_pause(self, _mock_suspended):
        conn = _make_conn()
        conn.td_logined = False
        conn.md_logined = False
        logger = FakeLogger()
        unhealthy = {
            'healthy': False,
            'issues': ['交易连接断开或登录失效', '发现 1 个僵尸订单'],
            'details': {},
        }

        with patch('auto_feishu_command.stop_command_receiver'), \
             patch('auto_feishu_command.start_command_receiver'), \
             patch('auto_scheduled_pause.sync_connection_suspend_state'), \
             patch('auto_circuit_breaker.CircuitBreaker'), \
             patch('straggle_execution.StrangleExecutor'), \
             patch('auto_health_check.HealthChecker') as mock_hc:
            mock_hc.return_value.check_now.return_value = unhealthy
            from merged_main_loop import run_merged_main_loop
            with patch('time.sleep', side_effect=KeyboardInterrupt):
                run_merged_main_loop(**_minimal_args(conn, logger))

        mock_hc.return_value.check_now.assert_not_called()
        health_logs = [m for lvl, m in logger.messages if '[健康]' in m]
        self.assertEqual(health_logs, [], health_logs)

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
        login_logs = [m for lvl, m in logger.messages if '未全部登录' in m or '未登录' in m]
        self.assertTrue(login_logs)


class TestRunReconcileStrangleExceptionFallback(unittest.TestCase):
    """P7: strangle reconcile 抛异常时，必须保守 halt 开仓而不是让异常
    向上 propagate 中断主循环。"""

    def test_exception_sets_halt_and_records_issue(self):
        from merged_main_loop import _run_reconcile

        conn = _make_conn()
        conn._runtime_state = {'_strangle_reconcile_issues': ['前一轮: SA gap']}
        strangle_logger = FakeLogger()
        spread_logger = FakeLogger()
        ledger = MagicMock()
        ledger.is_open_halted.return_value = False
        ledger.get_open_halt_reason.return_value = ''

        with patch(
            'merged_main_loop._prefetch_round_data',
            return_value=(None, None),
        ), patch(
            'strangle_reconcile_dual.reconcile_strangle_positions_dual',
            side_effect=RuntimeError('CTP 查询超时'),
        ):
            halt, issues, spread_halt, spread_issues = _run_reconcile(
                conn=conn,
                ledger=ledger,
                spread_tradeinfo=[],
                strangle_tradeinfo=[{'future': 'SA', 'month': '609'}],
                spread_logger=spread_logger,
                strangle_logger=strangle_logger,
                config={'dual_strategy': {'spread_execution_from_ledger': False}},
                str_cfg={'pause_open_on_reconcile_mismatch': True},
                dual={
                    'exclude_spread_from_strangle_reconcile': True,
                    'spread_execution_from_ledger': False,
                },
            )

        self.assertTrue(halt, '异常时应保守 halt')
        self.assertTrue(
            any('reconcile 异常' in m for m in issues),
            f'expected exception issue, got {issues}',
        )
        warning_logs = [m for lvl, m in strangle_logger.messages if lvl == 'WARNING']
        self.assertTrue(
            any('异常' in m for m in warning_logs),
            f'expected warning log, got {warning_logs}',
        )

    def test_normal_path_unaffected(self):
        from merged_main_loop import _run_reconcile

        conn = _make_conn()
        strangle_logger = FakeLogger()
        spread_logger = FakeLogger()
        ledger = MagicMock()
        ledger.is_open_halted.return_value = False
        ledger.get_open_halt_reason.return_value = ''

        with patch(
            'merged_main_loop._prefetch_round_data',
            return_value=(None, None),
        ), patch(
            'strangle_reconcile_dual.reconcile_strangle_positions_dual',
            return_value=(False, []),
        ):
            halt, issues, _, _ = _run_reconcile(
                conn=conn,
                ledger=ledger,
                spread_tradeinfo=[],
                strangle_tradeinfo=[{'future': 'SA', 'month': '609'}],
                spread_logger=spread_logger,
                strangle_logger=strangle_logger,
                config={'dual_strategy': {'spread_execution_from_ledger': False}},
                str_cfg={'pause_open_on_reconcile_mismatch': True},
                dual={
                    'exclude_spread_from_strangle_reconcile': True,
                    'spread_execution_from_ledger': False,
                },
            )

        self.assertFalse(halt)
        self.assertEqual(issues, [])


class TestRunReconcileSpreadExceptionFallback(unittest.TestCase):
    """与 strangle P7 对称：spread reconcile 抛异常时必须保守 halt 开仓 / 再平衡，
    并沿用上一轮 spread_issues 作为上下文，便于排障时看到历史差异。"""

    def test_exception_sets_halt_and_preserves_prev_issues(self):
        from merged_main_loop import _run_reconcile

        conn = _make_conn()
        conn._runtime_state = {
            '_spread_reconcile_issues': ['前一轮: SA609C1000 CTP=2 CSV=1'],
        }
        strangle_logger = FakeLogger()
        spread_logger = FakeLogger()
        ledger = MagicMock()
        ledger.is_open_halted.return_value = False
        ledger.get_open_halt_reason.return_value = ''

        with patch(
            'merged_main_loop._prefetch_round_data',
            return_value=(None, None),
        ), patch(
            'strangle_reconcile_dual.reconcile_strangle_positions_dual',
            return_value=(False, []),
        ), patch(
            'spread_reconcile.reconcile_spread_positions',
            side_effect=RuntimeError('CTP 持仓查询超时'),
        ):
            halt, issues, spread_halt, spread_issues = _run_reconcile(
                conn=conn,
                ledger=ledger,
                spread_tradeinfo=[{'future': 'SA', 'month': '609'}],
                strangle_tradeinfo=[],
                spread_logger=spread_logger,
                strangle_logger=strangle_logger,
                config={
                    'dual_strategy': {
                        'spread_execution_from_ledger': True,
                        'pause_spread_open_on_reconcile_mismatch': True,
                    },
                },
                str_cfg={'pause_open_on_reconcile_mismatch': True},
                dual={
                    'exclude_spread_from_strangle_reconcile': True,
                    'spread_execution_from_ledger': True,
                    'pause_spread_open_on_reconcile_mismatch': True,
                },
            )

        self.assertTrue(spread_halt, '价差对账异常时应保守 halt')
        self.assertTrue(
            any('reconcile 异常' in str(m) for m in spread_issues),
            f'expected exception issue, got {spread_issues}',
        )
        self.assertTrue(
            any('前一轮' in str(m) for m in spread_issues),
            f'expected prev issues preserved, got {spread_issues}',
        )
        self.assertTrue(
            conn._runtime_state.get('_spread_open_halted'),
            '_spread_open_halted 应被同步置 True',
        )
        warning_logs = [m for lvl, m in spread_logger.messages if lvl == 'WARNING']
        self.assertTrue(
            any('异常' in str(m) for m in warning_logs),
            f'expected warning log with exception, got {warning_logs}',
        )


class TestMainLoopConsecutiveErrors(unittest.TestCase):

    def _run_until_error(self, conn, logger, max_errors):
        conn.config['main_loop_max_consecutive_errors'] = max_errors
        conn.config['loop_interval'] = 0.01
        with patch('auto_feishu_command.stop_command_receiver'), \
             patch('auto_feishu_command.start_command_receiver'), \
             patch('auto_feishu_command.is_trading_paused', return_value=False), \
             patch('auto_scheduled_pause.sync_connection_suspend_state'), \
             patch('auto_scheduled_reconnect.check_scheduled_full_recovery', return_value=False), \
             patch('auto_circuit_breaker.CircuitBreaker'), \
             patch('straggle_execution.StrangleExecutor'), \
             patch('merged_main_loop.manage_future_price_readiness', side_effect=RuntimeError('boom')), \
             patch('time.sleep'):
            from merged_main_loop import run_merged_main_loop
            health = MagicMock()
            health.check_now.return_value = {'healthy': True}
            run_merged_main_loop(
                **_minimal_args(conn, logger),
                health_checker=health,
            )

    @patch('auto_scheduled_pause.is_connection_suspended', return_value=False)
    def test_consecutive_errors_trigger_process_restart(self, _mock_suspended):
        conn = _make_conn()
        logger = FakeLogger()
        with self.assertRaises(RuntimeError):
            self._run_until_error(conn, logger, max_errors=3)
        error_logs = [m for lvl, m in logger.messages if lvl == 'ERROR']
        self.assertTrue(
            any('触发进程级重启' in m for m in error_logs),
            error_logs,
        )


if __name__ == '__main__':
    unittest.main()
