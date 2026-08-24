"""spread_ledger_execution unit tests"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import autotrade_stubs

autotrade_stubs.ensure_merged_loop_stubs()
import ctp_bootstrap  # noqa: F401
from spread_dual_config import spread_execution_from_ledger


class TestSpreadLedgerExecution(unittest.TestCase):

    def test_spread_execution_flag_master(self):
        cfg = {
            'dual_strategy': {
                'use_spread_leg_claims': True,
                'spread_execution_from_ledger': True,
            },
        }
        self.assertTrue(spread_execution_from_ledger(cfg))

    def test_spread_execution_flag_legacy_close_alias(self):
        cfg = {
            'dual_strategy': {
                'use_spread_leg_claims': True,
                'spread_execution_from_ledger': False,
                'spread_close_from_ledger': True,
            },
        }
        self.assertTrue(spread_execution_from_ledger(cfg))

    def test_spread_execution_disabled_without_claims(self):
        cfg = {'dual_strategy': {'use_spread_leg_claims': False}}
        self.assertFalse(spread_execution_from_ledger(cfg))

    def test_module_imports(self):
        import spread_ledger_execution as sle
        self.assertTrue(callable(sle.install_spread_ledger_execution))
        self.assertTrue(callable(sle._rebind_analyze_consumers))
        self.assertTrue(callable(sle._rebind_module_attr))

    def test_leg_pairing_failure_leaves_not_installed(self):
        """只验证 leg pairing 分支；前置 install_spread_* 补丁 patch 掉
        （unit stub 环境下它们依赖 autotrade 真实模块属性）。"""
        import spread_ledger_execution as sle

        orig_installed = sle._INSTALLED
        sle._INSTALLED = False
        try:
            with patch.object(sle, 'install_spread_analyze_from_ledger'), \
                 patch.object(sle, 'install_spread_close_from_ledger'), \
                 patch.object(sle, 'install_spread_rebalance_from_ledger'), \
                 patch.object(sle, 'install_spread_process_symbol_halt'), \
                 patch.object(sle, 'install_spread_risk_check_exclusion'), \
                 patch.object(sle, 'install_spread_rebalance_close_a_exclusion'), \
                 patch(
                     'session_close_guard.install_session_close_guard',
                     return_value=True,
                 ), patch(
                     'strangle_leg_pairing.install_strangle_leg_pairing_patch',
                     return_value=False,
                 ), patch.object(sle, '_surface_leg_pairing_install_failure'):
                sle.install_spread_ledger_execution(
                    {'fail_fast_on_guard_install': False},
                )
            self.assertFalse(sle._INSTALLED)
        finally:
            sle._INSTALLED = orig_installed

    def test_session_close_guard_failure_surfaces(self):
        """收盘守卫安装失败不得静默：须走 surface（error+飞书+可 fail-fast）。

        前置的 install_spread_* 与 leg_pairing 均 patch 掉，只验证收盘守卫分支
        （unit stub 环境下前置补丁依赖 autotrade 真实模块属性）。
        """
        import spread_ledger_execution as sle

        orig_installed = sle._INSTALLED
        sle._INSTALLED = False
        try:
            with patch.object(sle, 'install_spread_analyze_from_ledger'), \
                 patch.object(sle, 'install_spread_close_from_ledger'), \
                 patch.object(sle, 'install_spread_rebalance_from_ledger'), \
                 patch.object(sle, 'install_spread_process_symbol_halt'), \
                 patch.object(sle, 'install_spread_risk_check_exclusion'), \
                 patch.object(sle, 'install_spread_rebalance_close_a_exclusion'), \
                 patch(
                     'strangle_leg_pairing.install_strangle_leg_pairing_patch',
                     return_value=True,
                 ), patch(
                     'session_close_guard.install_session_close_guard',
                     return_value=False,
                 ), patch(
                     'session_close_guard.get_install_error',
                     return_value='patch 失败',
                 ), patch.object(
                     sle, '_surface_session_close_guard_install_failure',
                 ) as surface:
                sle.install_spread_ledger_execution(
                    {'fail_fast_on_guard_install': False},
                )
            surface.assert_called_once()
            self.assertIn('patch 失败', surface.call_args.args[1])
        finally:
            sle._INSTALLED = orig_installed

    def test_session_close_guard_surface_fail_fast_exits(self):
        """fail_fast_on_guard_install=true 时收盘守卫失败应拒绝启动（exit 4）。"""
        import spread_ledger_execution as sle

        with patch('auto_feishu.send_feishu_message', return_value=True):
            with self.assertRaises(SystemExit) as ctx:
                sle._surface_session_close_guard_install_failure(
                    {'fail_fast_on_guard_install': True}, '模拟失败',
                )
        self.assertEqual(ctx.exception.code, 4)

    def test_session_close_guard_surface_non_fatal_by_default(self):
        import spread_ledger_execution as sle

        with patch('auto_feishu.send_feishu_message', return_value=True) as m:
            sle._surface_session_close_guard_install_failure(
                {'fail_fast_on_guard_install': False}, '模拟失败',
            )
        m.assert_called_once()
        self.assertIn('收盘守卫', m.call_args.args[0])


class TestProcessSymbolPatchReachesMainLoop(unittest.TestCase):
    """回归：复现真实启动顺序（先 import merged_main_loop，再 install patch），
    断言主循环会使用 patched process_symbol —— 即对账 halt 时走 close-only 路径。

    关键点：merged_main_loop 必须以模块属性方式 (`import auto_processor` +
    `auto_processor.process_symbol(...)`) 调用，否则 patch 不会被本模块看到。
    """

    def setUp(self):
        import auto_processor
        import merged_main_loop  # noqa: F401
        import spread_ledger_execution as sle

        self._sle = sle
        self._auto_processor = auto_processor
        self._merged_main_loop = merged_main_loop
        self._orig_auto_processor = auto_processor.process_symbol
        self._orig_installed = sle._INSTALLED
        self._orig_process_symbol = sle._ORIG_PROCESS_SYMBOL

    def tearDown(self):
        self._auto_processor.process_symbol = self._orig_auto_processor
        self._sle._ORIG_PROCESS_SYMBOL = self._orig_process_symbol
        self._sle._INSTALLED = self._orig_installed

    def test_main_loop_uses_module_attr_not_local_binding(self):
        """merged_main_loop must NOT keep a stale `from auto_processor import process_symbol`
        local binding. Either it doesn't define `process_symbol` at module scope, or
        its binding is the same identity as `auto_processor.process_symbol`.
        """
        if hasattr(self._merged_main_loop, 'process_symbol'):
            self.assertIs(
                self._merged_main_loop.process_symbol,
                self._auto_processor.process_symbol,
                'merged_main_loop.process_symbol must not pin a stale reference; '
                'use `auto_processor.process_symbol` instead.',
            )

    def test_patched_routes_to_close_only_when_halted(self):
        """Simulate the main-loop call path: after install, calling via
        ``auto_processor.process_symbol(...)`` (which is exactly what
        merged_main_loop now does) must route halted symbols into close-only."""
        cfg = {
            'dual_strategy': {
                'use_spread_leg_claims': True,
                'spread_execution_from_ledger': True,
            },
        }
        self._sle._ORIG_PROCESS_SYMBOL = None
        self._sle.install_spread_process_symbol_halt(cfg)

        close_only_calls = []

        def fake_close_only(conn, item, vix_engine, config, logger):
            close_only_calls.append(item.get('future'))
            return True

        self._sle._spread_close_only = fake_close_only

        conn = MagicMock()
        conn._runtime_state = {
            '_spread_open_halted': True,
            '_spread_open_halt_reason': 'unit test',
        }
        item = {'future': 'SA', 'month': '609'}
        logger = MagicMock()

        # 主循环就是这样调用的：import auto_processor; auto_processor.process_symbol(...)
        result = self._auto_processor.process_symbol(
            conn, item, MagicMock(), {}, logger, remaining_limit=0,
        )
        self.assertTrue(result)
        self.assertEqual(close_only_calls, ['SA'])

    def test_defensive_rebind_protects_from_imports(self):
        """Defensive coverage: if a downstream module ever does
        `from auto_processor import process_symbol`, install must rebind it too."""
        import sys
        import types

        fake_consumer = types.ModuleType('autoctp_test_fake_consumer')
        fake_consumer.process_symbol = self._orig_auto_processor
        sys.modules['autoctp_test_fake_consumer'] = fake_consumer
        prev_consumers = self._sle._PROCESS_SYMBOL_CONSUMERS
        self._sle._PROCESS_SYMBOL_CONSUMERS = prev_consumers + ('autoctp_test_fake_consumer',)
        try:
            self._sle._ORIG_PROCESS_SYMBOL = None
            self._sle.install_spread_process_symbol_halt({
                'dual_strategy': {
                    'use_spread_leg_claims': True,
                    'spread_execution_from_ledger': True,
                },
            })
            self.assertIs(
                fake_consumer.process_symbol,
                self._auto_processor.process_symbol,
            )
        finally:
            self._sle._PROCESS_SYMBOL_CONSUMERS = prev_consumers
            sys.modules.pop('autoctp_test_fake_consumer', None)


class _FakeStrangleLedger:
    def __init__(self, claims=None, unmatched=None):
        self._claims = claims or {}
        self._unmatched = unmatched or []

    def list_leg_claims(self):
        return dict(self._claims)

    def list_unmatched_legs(self):
        return list(self._unmatched)


class TestResolveSpreadPositionsForRisk(unittest.TestCase):
    """共享口径：check_risk_limits / rebalance 平A 候选都经此解析，只数价差腿。"""

    def _conn(self):
        conn = MagicMock()
        conn._runtime_state = {}
        conn._normalize_month = lambda symbol, month: month
        return conn

    def test_dual_isolation_active_default_true(self):
        import spread_ledger_execution as sle
        self.assertTrue(sle._dual_isolation_active({'dual_strategy': {}}))

    def test_dual_isolation_inactive_when_all_off(self):
        import spread_ledger_execution as sle
        cfg = {'dual_strategy': {
            'use_spread_leg_claims': False,
            'spread_execution_from_ledger': False,
            'spread_close_from_ledger': False,
            'exclude_strangle_from_spread_positions': False,
        }}
        self.assertFalse(sle._dual_isolation_active(cfg))

    def test_store_path_returns_only_ledger_legs(self):
        import spread_ledger_execution as sle
        from spread_ledger import SpreadLegStore

        conn = self._conn()
        store = SpreadLegStore()
        store.set_leg_claims({'SA609C2400': 1, 'SA609C2500': -2})
        conn._runtime_state['_spread_leg_store'] = store
        raw = [
            {'instrument': 'SA609C2400', 'direction': '2', 'position': 99},
            {'instrument': 'SA609C9999', 'direction': '2', 'position': 20},
        ]
        out = sle._resolve_spread_positions_for_risk(
            conn, raw, 'SA', '609', {'dual_strategy': {}},
        )
        by_inst = {p['instrument']: int(p['position']) for p in out}
        self.assertEqual(by_inst.get('SA609C2400'), 1)
        self.assertNotIn('SA609C9999', by_inst)

    def test_no_store_excludes_strangle_long(self):
        import spread_ledger_execution as sle

        conn = self._conn()
        conn._runtime_state['_strangle_ledger'] = _FakeStrangleLedger(
            claims={'MA609C3650': 20},
        )
        raw = [
            {'instrument': 'MA609C3650', 'direction': '2', 'position': 20},
            {'instrument': 'MA609C3700', 'direction': '3', 'position': 10},
        ]
        out = sle._resolve_spread_positions_for_risk(
            conn, raw, 'MA', '609', {'dual_strategy': {}},
        )
        by_inst = {p['instrument']: int(p['position']) for p in out}
        self.assertNotIn('MA609C3650', by_inst)
        self.assertEqual(by_inst.get('MA609C3700'), 10)


class TestRebindModuleAttrHelper(unittest.TestCase):

    def test_returns_0_when_module_missing(self):
        import spread_ledger_execution as sle
        self.assertEqual(
            sle._rebind_module_attr('definitely_no_such_module', 'x', None),
            0,
        )

    def test_returns_1_when_rebinds(self):
        import sys
        import types
        import spread_ledger_execution as sle

        fake = types.ModuleType('autoctp_test_fake_module')
        fake.fn = lambda: 'orig'
        sys.modules['autoctp_test_fake_module'] = fake
        try:
            replacement = lambda: 'new'
            self.assertEqual(
                sle._rebind_module_attr('autoctp_test_fake_module', 'fn', replacement),
                1,
            )
            self.assertIs(fake.fn, replacement)
        finally:
            sys.modules.pop('autoctp_test_fake_module', None)


if __name__ == '__main__':
    unittest.main()
