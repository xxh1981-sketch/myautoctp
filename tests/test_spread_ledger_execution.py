"""spread_ledger_execution unit tests"""

import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
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
