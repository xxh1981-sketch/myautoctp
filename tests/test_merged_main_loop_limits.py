"""merged_main_loop daily limit / margin halt tests"""

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
        'margin_recheck_interval_sec': 0,
        'scheduled_full_recovery_enabled': False,
        'scheduled_session_pause_enabled': False,
    }
    conn.get_filled_open_order_count = MagicMock(return_value=5)
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


class TestDailyLimitDoesNotSkipClose(unittest.TestCase):

    @patch('auto_processor.process_symbol', return_value=False)
    @patch('margin_check.check_margin_status', return_value=('ok', ''))
    @patch('auto_feishu_command.stop_command_receiver')
    @patch('auto_feishu_command.start_command_receiver')
    @patch('auto_scheduled_pause.sync_connection_suspend_state')
    @patch('auto_circuit_breaker.CircuitBreaker')
    @patch('straggle_execution.StrangleExecutor')
    @patch('auto_health_check.HealthChecker')
    def test_spread_at_limit_still_scans_symbols(
        self,
        mock_hc,
        mock_exec,
        mock_cb,
        mock_sync,
        mock_start,
        mock_stop,
        mock_margin_status,
        mock_process,
    ):
        conn = _make_conn()
        logger = FakeLogger()
        mock_hc.return_value.check_now.return_value = {'healthy': True}
        ledger = MagicMock()
        ledger.get_daily_buy_amount.return_value = 0
        ledger.list_unmatched_legs.return_value = []
        ledger.is_open_halted.return_value = False

        from merged_main_loop import run_merged_main_loop
        with patch('spread_fill_sync.count_spread_filled_open_orders', return_value=5), \
             patch('time.sleep', side_effect=KeyboardInterrupt):
            run_merged_main_loop(
                conn=conn,
                spread_tradeinfo=[{'future': 'SA', 'month': '609'}],
                strangle_tradeinfo=[],
                combined_tradeinfo=[],
                vix_engine=MagicMock(),
                config={
                    **conn.config,
                    'dual_strategy': {
                        'reconcile_interval_sec': 0,
                        'journal_daily_shards': False,
                    },
                },
                logger=logger,
                ledger=ledger,
            )

        self.assertGreaterEqual(mock_process.call_count, 1)


class TestFillCountExceptionStillScansClose(unittest.TestCase):
    """成交笔数查询抛异常时与 fc=None 一致：保守禁新开，仍扫描平仓。"""

    @patch('auto_processor.process_symbol', return_value=False)
    @patch('margin_check.check_margin_status', return_value=('ok', ''))
    @patch('auto_feishu_command.stop_command_receiver')
    @patch('auto_feishu_command.start_command_receiver')
    @patch('auto_scheduled_pause.sync_connection_suspend_state')
    @patch('auto_circuit_breaker.CircuitBreaker')
    @patch('straggle_execution.StrangleExecutor')
    @patch('auto_health_check.HealthChecker')
    def test_query_exception_still_scans_symbols(
        self,
        mock_hc,
        mock_exec,
        mock_cb,
        mock_sync,
        mock_start,
        mock_stop,
        mock_margin_status,
        mock_process,
    ):
        conn = _make_conn()
        logger = FakeLogger()
        mock_hc.return_value.check_now.return_value = {'healthy': True}
        ledger = MagicMock()
        ledger.get_daily_buy_amount.return_value = 0
        ledger.list_unmatched_legs.return_value = []
        ledger.is_open_halted.return_value = False

        from merged_main_loop import run_merged_main_loop

        def _raise_query(*args, **kwargs):
            raise RuntimeError('CTP trade query timeout')

        with patch(
            'spread_fill_sync.count_spread_filled_open_orders',
            side_effect=_raise_query,
        ), patch('time.sleep', side_effect=KeyboardInterrupt):
            run_merged_main_loop(
                conn=conn,
                spread_tradeinfo=[{'future': 'SA', 'month': '609'}],
                strangle_tradeinfo=[],
                combined_tradeinfo=[],
                vix_engine=MagicMock(),
                config={
                    **conn.config,
                    'dual_strategy': {
                        'reconcile_interval_sec': 0,
                        'journal_daily_shards': False,
                    },
                },
                logger=logger,
                ledger=ledger,
            )

        self.assertGreaterEqual(mock_process.call_count, 1)
        self.assertTrue(
            any('成交查询异常' in str(msg) for _, msg in logger.messages),
        )


class TestSpreadFilledRefreshFailureConservative(unittest.TestCase):
    """品种扫描后刷新日笔数失败时，不用 +1 估计，本轮禁新开。"""

    @patch('merged_main_loop._run_reconcile', return_value=(False, [], False, []))
    @patch('auto_processor.process_symbol', return_value=True)
    @patch('margin_check.check_margin_status', return_value=('ok', ''))
    @patch('auto_feishu_command.stop_command_receiver')
    @patch('auto_feishu_command.start_command_receiver')
    @patch('auto_scheduled_pause.sync_connection_suspend_state')
    @patch('auto_circuit_breaker.CircuitBreaker')
    @patch('straggle_execution.StrangleExecutor')
    @patch('auto_health_check.HealthChecker')
    def test_refresh_exception_disables_open_for_round(
        self,
        mock_hc, mock_exec, mock_cb, mock_sync, mock_start, mock_stop,
        mock_margin, mock_process, mock_recon,
    ):
        conn = _make_conn()
        conn.config['daily_trade_limit'] = 10
        logger = FakeLogger()
        mock_hc.return_value.check_now.return_value = {'healthy': True}
        ledger = MagicMock()
        ledger.get_daily_buy_amount.return_value = 0
        ledger.list_unmatched_legs.return_value = []
        ledger.is_open_halted.return_value = False

        call_n = {'n': 0}

        def _count(*args, **kwargs):
            call_n['n'] += 1
            if call_n['n'] == 1:
                return 0
            raise RuntimeError('refresh failed')

        from merged_main_loop import run_merged_main_loop
        with patch(
            'spread_fill_sync.count_spread_filled_open_orders',
            side_effect=_count,
        ), patch('time.sleep', side_effect=KeyboardInterrupt):
            run_merged_main_loop(
                conn=conn,
                spread_tradeinfo=[
                    {'future': 'SA', 'month': '609'},
                    {'future': 'FG', 'month': '609'},
                ],
                strangle_tradeinfo=[],
                combined_tradeinfo=[],
                vix_engine=MagicMock(),
                config={
                    **conn.config,
                    'dual_strategy': {
                        'reconcile_interval_sec': 0,
                        'journal_daily_shards': False,
                    },
                },
                logger=logger,
                ledger=ledger,
            )

        self.assertTrue(
            any('价差日笔数刷新失败' in str(msg) for _, msg in logger.messages),
        )
        # 第二个品种仍扫描，但 remaining_limit 应为 0（禁新开）
        self.assertGreaterEqual(mock_process.call_count, 2)
        second_call = mock_process.call_args_list[1]
        self.assertEqual(second_call.kwargs.get('remaining_limit'), 0)


class TestDailyLimitWarningOnlyOnRealLimit(unittest.TestCase):
    """`日笔数达限` warning 必须只在 fc >= daily_trade_limit 时打；
    reconcile / margin halt 导致的 spread_open_ok=False 不应被误报为日限达限。"""

    def _has_limit_warning(self, logger) -> bool:
        # StrategyLoggerAdapter 通过 .log() 转发，故 FakeLogger 记为 'LOG'；
        # 直接按消息文本匹配即可（前缀 "[价差] " 不影响 in 判断）。
        return any('日笔数达限' in str(msg) for _, msg in logger.messages)

    @patch(
        'merged_main_loop._run_reconcile',
        return_value=(False, [], True, ['SA609C1000 CTP=2 CSV=1']),
    )
    @patch('auto_processor.process_symbol', return_value=False)
    @patch('margin_check.check_margin_status', return_value=('ok', ''))
    @patch('auto_feishu_command.stop_command_receiver')
    @patch('auto_feishu_command.start_command_receiver')
    @patch('auto_scheduled_pause.sync_connection_suspend_state')
    @patch('auto_circuit_breaker.CircuitBreaker')
    @patch('straggle_execution.StrangleExecutor')
    @patch('auto_health_check.HealthChecker')
    def test_reconcile_halt_does_not_log_daily_limit(
        self,
        mock_hc, mock_exec, mock_cb, mock_sync, mock_start, mock_stop,
        mock_margin, mock_process, mock_recon,
    ):
        conn = _make_conn()
        logger = FakeLogger()
        mock_hc.return_value.check_now.return_value = {'healthy': True}
        ledger = MagicMock()
        ledger.get_daily_buy_amount.return_value = 0
        ledger.list_unmatched_legs.return_value = []
        ledger.is_open_halted.return_value = False

        from merged_main_loop import run_merged_main_loop
        # fc=0 (远未达日限) 但价差对账 halt → 不应出现"日笔数达限"
        with patch('spread_fill_sync.count_spread_filled_open_orders', return_value=0), \
             patch('time.sleep', side_effect=KeyboardInterrupt):
            run_merged_main_loop(
                conn=conn,
                spread_tradeinfo=[{'future': 'SA', 'month': '609'}],
                strangle_tradeinfo=[],
                combined_tradeinfo=[],
                vix_engine=MagicMock(),
                config={
                    **conn.config,
                    'dual_strategy': {
                        'reconcile_interval_sec': 0,
                        'journal_daily_shards': False,
                        'pause_spread_open_on_reconcile_mismatch': True,
                    },
                },
                logger=logger,
                ledger=ledger,
            )

        self.assertFalse(
            self._has_limit_warning(logger),
            'reconcile halt 时不应误报"日笔数达限": '
            f'{[m for l, m in logger.messages if l == "WARNING"]}',
        )

    @patch('merged_main_loop._run_reconcile', return_value=(False, [], False, []))
    @patch('auto_processor.process_symbol', return_value=False)
    @patch('margin_check.check_margin_status', return_value=('over_limit', '保证金超限'))
    @patch('auto_feishu_command.stop_command_receiver')
    @patch('auto_feishu_command.start_command_receiver')
    @patch('auto_scheduled_pause.sync_connection_suspend_state')
    @patch('auto_circuit_breaker.CircuitBreaker')
    @patch('straggle_execution.StrangleExecutor')
    @patch('auto_health_check.HealthChecker')
    def test_margin_halt_does_not_log_daily_limit(
        self,
        mock_hc, mock_exec, mock_cb, mock_sync, mock_start, mock_stop,
        mock_margin, mock_process, mock_recon,
    ):
        conn = _make_conn()
        logger = FakeLogger()
        mock_hc.return_value.check_now.return_value = {'healthy': True}
        ledger = MagicMock()
        ledger.get_daily_buy_amount.return_value = 0
        ledger.list_unmatched_legs.return_value = []
        ledger.is_open_halted.return_value = False

        from merged_main_loop import run_merged_main_loop
        with patch('spread_fill_sync.count_spread_filled_open_orders', return_value=0), \
             patch('time.sleep', side_effect=KeyboardInterrupt):
            run_merged_main_loop(
                conn=conn,
                spread_tradeinfo=[{'future': 'SA', 'month': '609'}],
                strangle_tradeinfo=[],
                combined_tradeinfo=[],
                vix_engine=MagicMock(),
                config={
                    **conn.config,
                    'global_margin_limit': 1000,
                    'dual_strategy': {
                        'reconcile_interval_sec': 0,
                        'journal_daily_shards': False,
                    },
                },
                logger=logger,
                ledger=ledger,
            )

        self.assertFalse(
            self._has_limit_warning(logger),
            '保证金 halt 时不应误报"日笔数达限": '
            f'{[m for l, m in logger.messages if l == "WARNING"]}',
        )

    @patch('merged_main_loop._run_reconcile', return_value=(False, [], False, []))
    @patch('auto_processor.process_symbol', return_value=False)
    @patch('margin_check.check_margin_status', return_value=('ok', ''))
    @patch('auto_feishu_command.stop_command_receiver')
    @patch('auto_feishu_command.start_command_receiver')
    @patch('auto_scheduled_pause.sync_connection_suspend_state')
    @patch('auto_circuit_breaker.CircuitBreaker')
    @patch('straggle_execution.StrangleExecutor')
    @patch('auto_health_check.HealthChecker')
    def test_real_limit_does_log_warning(
        self,
        mock_hc, mock_exec, mock_cb, mock_sync, mock_start, mock_stop,
        mock_margin, mock_process, mock_recon,
    ):
        conn = _make_conn()
        logger = FakeLogger()
        mock_hc.return_value.check_now.return_value = {'healthy': True}
        ledger = MagicMock()
        ledger.get_daily_buy_amount.return_value = 0
        ledger.list_unmatched_legs.return_value = []
        ledger.is_open_halted.return_value = False

        from merged_main_loop import run_merged_main_loop
        # fc=5 == daily_trade_limit=5 → 真正达限
        with patch('spread_fill_sync.count_spread_filled_open_orders', return_value=5), \
             patch('time.sleep', side_effect=KeyboardInterrupt):
            run_merged_main_loop(
                conn=conn,
                spread_tradeinfo=[{'future': 'SA', 'month': '609'}],
                strangle_tradeinfo=[],
                combined_tradeinfo=[],
                vix_engine=MagicMock(),
                config={
                    **conn.config,
                    'dual_strategy': {
                        'reconcile_interval_sec': 0,
                        'journal_daily_shards': False,
                    },
                },
                logger=logger,
                ledger=ledger,
            )

        self.assertTrue(
            self._has_limit_warning(logger),
            '真正达日限时应该打 warning',
        )


class TestMarginUnknownPreservesPrevState(unittest.TestCase):
    """check_margin_status returning 'unknown' (CTP query failed N times) must
    NOT flip _margin_halt_open / _margin_halt_reason to a fresh state — that
    would either falsely halt a healthy account or falsely un-halt one already
    over the limit. Behavior: keep the previous values verbatim."""

    @patch('margin_check.check_margin_status', return_value=('unknown', '查询失败'))
    @patch('auto_processor.process_symbol', return_value=False)
    @patch('auto_feishu_command.stop_command_receiver')
    @patch('auto_feishu_command.start_command_receiver')
    @patch('auto_scheduled_pause.sync_connection_suspend_state')
    @patch('auto_circuit_breaker.CircuitBreaker')
    @patch('straggle_execution.StrangleExecutor')
    @patch('auto_health_check.HealthChecker')
    def test_unknown_preserves_previous_halt(
        self,
        mock_hc,
        mock_exec,
        mock_cb,
        mock_sync,
        mock_start,
        mock_stop,
        mock_process,
        mock_status,
    ):
        conn = _make_conn()
        conn._runtime_state['_margin_halt_open'] = True
        conn._runtime_state['_margin_halt_reason'] = '保证金超限 (前一轮)'
        logger = FakeLogger()
        mock_hc.return_value.check_now.return_value = {'healthy': True}
        ledger = MagicMock()
        ledger.get_daily_buy_amount.return_value = 0
        ledger.list_unmatched_legs.return_value = []
        ledger.is_open_halted.return_value = True
        ledger.get_open_halt_reason.return_value = '保证金超限 (前一轮)'

        from merged_main_loop import run_merged_main_loop
        with patch('spread_fill_sync.count_spread_filled_open_orders', return_value=0), \
             patch('time.sleep', side_effect=KeyboardInterrupt):
            run_merged_main_loop(
                conn=conn,
                spread_tradeinfo=[{'future': 'SA', 'month': '609'}],
                strangle_tradeinfo=[],
                combined_tradeinfo=[],
                vix_engine=MagicMock(),
                config={
                    **conn.config,
                    'global_margin_limit': 100000,
                    'dual_strategy': {
                        'reconcile_interval_sec': 0,
                        'journal_daily_shards': False,
                    },
                },
                logger=logger,
                ledger=ledger,
            )

        self.assertTrue(conn._runtime_state['_margin_halt_open'])
        self.assertEqual(
            conn._runtime_state['_margin_halt_reason'], '保证金超限 (前一轮)',
        )


class TestSyncStrangleOpenHalt(unittest.TestCase):
    """`_sync_strangle_open_halt` is the single source of truth for ledger
    open_halted given (reconcile halt, margin halt)."""

    def _fake_ledger(self, halted=False, reason=''):
        ledger = MagicMock()
        state = {'halted': halted, 'reason': reason}
        ledger.is_open_halted.side_effect = lambda: state['halted']
        ledger.get_open_halt_reason.side_effect = lambda: state['reason']

        def _set(h, r=''):
            state['halted'] = bool(h)
            state['reason'] = r or ''
        ledger.set_open_halt.side_effect = _set
        return ledger, state

    def test_disabled_pause_is_noop(self):
        from merged_main_loop import _sync_strangle_open_halt
        conn = _make_conn()
        ledger, state = self._fake_ledger()
        _sync_strangle_open_halt(
            conn, ledger, {'pause_open_on_reconcile_mismatch': False},
        )
        ledger.set_open_halt.assert_not_called()

    def test_margin_halt_sets_ledger_with_reason(self):
        from merged_main_loop import _sync_strangle_open_halt
        conn = _make_conn()
        conn._runtime_state['_margin_halt_open'] = True
        conn._runtime_state['_margin_halt_reason'] = '保证金超限 (限额 1000)'
        ledger, state = self._fake_ledger()
        _sync_strangle_open_halt(conn, ledger, {})
        self.assertTrue(state['halted'])
        self.assertIn('保证金', state['reason'])

    def test_reconcile_reason_wins_over_margin(self):
        from merged_main_loop import _sync_strangle_open_halt
        conn = _make_conn()
        conn._runtime_state['_strangle_reconcile_halt'] = True
        conn._runtime_state['_strangle_reconcile_issues'] = [
            'SA609C1000 gap=1', 'SA609C1100 gap=2',
        ]
        conn._runtime_state['_margin_halt_open'] = True
        conn._runtime_state['_margin_halt_reason'] = '保证金超限'
        ledger, state = self._fake_ledger()
        _sync_strangle_open_halt(conn, ledger, {})
        self.assertTrue(state['halted'])
        self.assertIn('SA609C1000', state['reason'])
        self.assertNotIn('保证金', state['reason'])

    def test_clears_when_both_safe(self):
        from merged_main_loop import _sync_strangle_open_halt
        conn = _make_conn()
        conn._runtime_state['_strangle_reconcile_halt'] = False
        conn._runtime_state['_strangle_reconcile_issues'] = []
        conn._runtime_state['_margin_halt_open'] = False
        ledger, state = self._fake_ledger(halted=True, reason='old')
        _sync_strangle_open_halt(conn, ledger, {})
        self.assertFalse(state['halted'])
        self.assertEqual(state['reason'], '')

    def test_idempotent_when_unchanged(self):
        from merged_main_loop import _sync_strangle_open_halt
        conn = _make_conn()
        conn._runtime_state['_margin_halt_open'] = True
        conn._runtime_state['_margin_halt_reason'] = '保证金超限'
        ledger, _ = self._fake_ledger(halted=True, reason='保证金超限')
        _sync_strangle_open_halt(conn, ledger, {})
        ledger.set_open_halt.assert_not_called()


class TestRebalanceCloseOnlyOnStrangleReconcileHalt(unittest.TestCase):
    """对账 halt 时主循环 rebalance 必须走 close-only 路径——autostraggle 的
    ``run_rebalance`` 不读 ``ledger.is_open_halted()``，否则会照常执行
    ``awaiting_phase2`` 的开仓阶段二，违反"对账 halt = close-only"约定。"""

    def _set_recon_halt(self, conn):
        """side_effect to simulate _run_reconcile flipping runtime_state on."""

        def _inner(*args, **kw):
            conn._runtime_state['_strangle_reconcile_halt'] = True
            conn._runtime_state['_strangle_reconcile_issues'] = ['SA gap']
            return (True, ['SA gap'], False, [])

        return _inner

    @patch('strangle_rebalance_close_only.run_close_only_rebalance', return_value=1)
    @patch('straggle_processor.process_strangle_symbol', return_value=False)
    @patch('auto_processor.process_symbol', return_value=False)
    @patch('margin_check.check_margin_status', return_value=('ok', ''))
    @patch('auto_feishu_command.is_trading_paused', return_value=False)
    @patch('auto_feishu_command.stop_command_receiver')
    @patch('auto_feishu_command.start_command_receiver')
    @patch('auto_scheduled_pause.sync_connection_suspend_state')
    @patch('auto_circuit_breaker.CircuitBreaker')
    @patch('straggle_execution.StrangleExecutor')
    @patch('auto_health_check.HealthChecker')
    def test_reconcile_halt_uses_close_only_rebalance(
        self, mock_hc, mock_exec, mock_cb, mock_sync, mock_start, mock_stop,
        mock_paused, mock_margin, mock_process, mock_strangle_proc,
        mock_close_only,
    ):
        conn = _make_conn()
        conn.get_filled_open_order_count = MagicMock(return_value=0)
        logger = FakeLogger()
        mock_hc.return_value.check_now.return_value = {'healthy': True}
        ledger = MagicMock()
        ledger.get_daily_buy_amount.return_value = 0
        ledger.list_unmatched_legs.return_value = [
            {'symbol': 'sa', 'month': '2608', 'kind': 'close_chp_pending'},
        ]
        ledger.is_open_halted.return_value = False
        ledger.get_open_halt_reason.return_value = ''

        executor_inst = mock_exec.return_value
        from merged_main_loop import run_merged_main_loop
        with patch(
            'merged_main_loop._run_reconcile',
            side_effect=self._set_recon_halt(conn),
        ), patch(
            'spread_fill_sync.count_spread_filled_open_orders', return_value=0,
        ), patch('time.sleep', side_effect=KeyboardInterrupt):
            run_merged_main_loop(
                conn=conn,
                spread_tradeinfo=[],
                strangle_tradeinfo=[],
                combined_tradeinfo=[],
                vix_engine=MagicMock(),
                config={
                    **conn.config,
                    'dual_strategy': {
                        'reconcile_interval_sec': 0,
                        'journal_daily_shards': False,
                    },
                    'strangle': {
                        'daily_buy_limit_yuan': 300000,
                    },
                },
                logger=logger,
                ledger=ledger,
            )

        mock_close_only.assert_called()
        executor_inst.run_rebalance.assert_not_called()
        info_logs = [m for _, m in logger.messages if '对账 halt' in str(m)]
        self.assertTrue(
            info_logs,
            f'expected "对账 halt" in info log, got '
            f'{[m for _, m in logger.messages]}',
        )

    @patch('strangle_rebalance_close_only.run_close_only_rebalance', return_value=0)
    @patch('straggle_processor.process_strangle_symbol', return_value=False)
    @patch('auto_processor.process_symbol', return_value=False)
    @patch('margin_check.check_margin_status', return_value=('over_limit', '保证金超限'))
    @patch('auto_feishu_command.is_trading_paused', return_value=False)
    @patch('auto_feishu_command.stop_command_receiver')
    @patch('auto_feishu_command.start_command_receiver')
    @patch('auto_scheduled_pause.sync_connection_suspend_state')
    @patch('auto_circuit_breaker.CircuitBreaker')
    @patch('straggle_execution.StrangleExecutor')
    @patch('auto_health_check.HealthChecker')
    def test_margin_plus_reconcile_halt_combined_reason(
        self, mock_hc, mock_exec, mock_cb, mock_sync, mock_start, mock_stop,
        mock_paused, mock_margin, mock_process, mock_strangle_proc,
        mock_close_only,
    ):
        conn = _make_conn()
        conn.get_filled_open_order_count = MagicMock(return_value=0)
        logger = FakeLogger()
        mock_hc.return_value.check_now.return_value = {'healthy': True}
        ledger = MagicMock()
        ledger.get_daily_buy_amount.return_value = 0
        ledger.list_unmatched_legs.return_value = [
            {'symbol': 'sa', 'month': '2608', 'kind': 'close_chp_pending'},
        ]
        ledger.is_open_halted.return_value = False
        ledger.get_open_halt_reason.return_value = ''

        executor_inst = mock_exec.return_value
        from merged_main_loop import run_merged_main_loop

        def _set_both(*args, **kw):
            conn._runtime_state['_strangle_reconcile_halt'] = True
            conn._runtime_state['_strangle_reconcile_issues'] = ['SA gap']
            return (True, ['SA gap'], False, [])

        with patch(
            'merged_main_loop._run_reconcile', side_effect=_set_both,
        ), patch(
            'spread_fill_sync.count_spread_filled_open_orders', return_value=0,
        ), patch('time.sleep', side_effect=KeyboardInterrupt):
            run_merged_main_loop(
                conn=conn,
                spread_tradeinfo=[],
                strangle_tradeinfo=[],
                combined_tradeinfo=[],
                vix_engine=MagicMock(),
                config={
                    **conn.config,
                    'global_margin_limit': 1000,
                    'dual_strategy': {
                        'reconcile_interval_sec': 0,
                        'journal_daily_shards': False,
                    },
                    'strangle': {
                        'daily_buy_limit_yuan': 300000,
                    },
                },
                logger=logger,
                ledger=ledger,
            )

        mock_close_only.assert_called()
        executor_inst.run_rebalance.assert_not_called()
        combined = [m for _, m in logger.messages if '保证金超限+对账 halt' in str(m)]
        self.assertTrue(
            combined,
            f'expected combined halt reason in log, got '
            f'{[m for _, m in logger.messages]}',
        )


if __name__ == '__main__':
    unittest.main()
