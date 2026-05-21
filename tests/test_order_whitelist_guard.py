"""order_whitelist_guard unit tests.

The patch swaps ``OrderManager.send_order`` for a guarded wrapper. We replace
the upstream method with a tracer first, then install the guard, so the
wrapper delegates to our tracer on pass (verifying both the gate and the
delegation semantics).
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ctp_bootstrap  # noqa: F401

import order_whitelist_guard
from order_whitelist_guard import (
    audit_target_months_coverage,
    install_send_order_month_guard,
)


class FakeLogger:
    def __init__(self):
        self.errors = []

    def error(self, msg, *a, **k):
        self.errors.append(msg)

    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def debug(self, *a, **k):
        pass


class FakeConn:
    def __init__(self, symbols, target_months):
        self.symbols = list(symbols)
        self.target_months = dict(target_months)

    def _normalize_month(self, sym, month):
        return month


class TestOrderWhitelistGuard(unittest.TestCase):

    def setUp(self):
        import auto_order_manager as aom

        order_whitelist_guard._INSTALLED = False
        self._original_send_order = aom.OrderManager.send_order
        self._delegated_calls = []

        def tracer(self2, instrument, direction, volume, price,
                   offset='0', hedge='1', assert_no_pending=False,
                   strategy='spread'):
            self._delegated_calls.append(instrument)
            return 999, '999'

        aom.OrderManager.send_order = tracer
        install_send_order_month_guard()
        self.OrderManager = aom.OrderManager

    def tearDown(self):
        import auto_order_manager as aom
        aom.OrderManager.send_order = self._original_send_order
        order_whitelist_guard._INSTALLED = False

    def _make_mgr(self, conn):
        mgr = self.OrderManager.__new__(self.OrderManager)
        mgr.conn = conn
        mgr.logger = FakeLogger()
        return mgr

    def test_accepts_option_in_target_month(self):
        conn = FakeConn(['sa'], {'sa': ['2608']})
        mgr = self._make_mgr(conn)
        ref, _ = mgr.send_order('SA2608C2400', '0', 1, 1.0)
        self.assertEqual(ref, 999)
        self.assertEqual(self._delegated_calls, ['SA2608C2400'])

    def test_rejects_option_in_non_target_month(self):
        conn = FakeConn(['sa'], {'sa': ['2608']})
        mgr = self._make_mgr(conn)
        ref, _ = mgr.send_order('SA2609C2400', '0', 1, 1.0)
        self.assertIsNone(ref)
        self.assertEqual(self._delegated_calls, [])
        self.assertTrue(
            any('非目标月份' in e for e in mgr.logger.errors)
        )

    def test_rejects_future_contract(self):
        conn = FakeConn(['sa'], {'sa': ['2608']})
        mgr = self._make_mgr(conn)
        ref, _ = mgr.send_order('SA2608', '0', 1, 1.0)
        self.assertIsNone(ref)
        self.assertEqual(self._delegated_calls, [])
        self.assertTrue(any('非期权' in e for e in mgr.logger.errors))

    def test_rejects_unparsable_month(self):
        """合约月份无法解析（前缀后无数字段）→ 拒绝。"""
        conn = FakeConn(['abc'], {'abc': ['2608']})
        mgr = self._make_mgr(conn)
        # 'ABC-CALL' 通过 option_like 但无 ^[a-z]+\d{3,4} 月份段。
        ref, _ = mgr.send_order('ABC-C-2400', '0', 1, 1.0)
        self.assertIsNone(ref)
        self.assertEqual(self._delegated_calls, [])

    def test_no_target_months_falls_through(self):
        """没有 target_months 配置时不阻断（向后兼容）。"""
        conn = FakeConn(['sa'], {})
        mgr = self._make_mgr(conn)
        ref, _ = mgr.send_order('SA2608C2400', '0', 1, 1.0)
        self.assertEqual(ref, 999)

    def test_dash_option_format(self):
        """支持 IO2604-C-4000 这类带横线的格式。"""
        conn = FakeConn(['io'], {'io': ['2604']})
        mgr = self._make_mgr(conn)
        ref, _ = mgr.send_order('IO2604-C-4000', '0', 1, 1.0)
        self.assertEqual(ref, 999)

    def test_install_is_idempotent(self):
        """重复 install 不会叠加多层 wrapper。"""
        install_send_order_month_guard()
        install_send_order_month_guard()
        conn = FakeConn(['sa'], {'sa': ['2608']})
        mgr = self._make_mgr(conn)
        mgr.send_order('SA2608C2400', '0', 1, 1.0)
        self.assertEqual(len(self._delegated_calls), 1)

    def test_install_returns_true_when_active(self):
        """成功安装时返回 True，且 get_install_error 返回 None。"""
        # setUp 已经 install 过，再调一次应得 True（幂等分支）
        self.assertTrue(install_send_order_month_guard())
        self.assertIsNone(order_whitelist_guard.get_install_error())
        self.assertTrue(order_whitelist_guard.is_installed())

    def test_audit_target_months_coverage(self):
        conn = FakeConn(['sa', 'io'], {'sa': ['2608']})
        missing = audit_target_months_coverage(
            conn,
            [{'future': 'SA', 'month': '2608'}],
            [{'future': 'IO', 'month': '2604'}],
        )
        self.assertEqual(missing, ['io'])
        self.assertEqual(
            audit_target_months_coverage(
                conn,
                [{'future': 'SA', 'month': '2608'}],
                [],
            ),
            [],
        )

    def test_alert_passes_conn_config(self):
        """拦截发单时飞书告警须带 conn.config，与项目其他飞书调用一致。"""
        from unittest.mock import patch

        conn = FakeConn(['sa'], {'sa': ['2608']})
        conn.config = {'feishu_webhook': 'http://example/test'}
        mgr = self._make_mgr(conn)

        captured = {}

        def fake_send(message, config=None):
            captured['message'] = message
            captured['config'] = config

        with patch.dict(
            sys.modules,
            {
                'auto_feishu': type(
                    'M', (), {'send_feishu_message': staticmethod(fake_send)}
                )(),
            },
        ):
            mgr.send_order('SA2609C2400', '0', 1, 1.0)

        self.assertIn('非目标月份', captured.get('message', ''))
        self.assertIs(captured.get('config'), conn.config)

    def test_feishu_alert_cooldown_same_wrong_month(self):
        """同品种同错月重复拦截：日志每次打，飞书冷却期内只发一次。"""
        from unittest.mock import patch

        conn = FakeConn(['sa'], {'sa': ['2608']})
        conn.config = {'whitelist_feishu_cooldown_sec': 300}
        conn._runtime_state = {}
        mgr = self._make_mgr(conn)
        sent = []

        def fake_send(message, config=None):
            sent.append(message)

        with patch.dict(
            sys.modules,
            {
                'auto_feishu': type(
                    'M', (), {'send_feishu_message': staticmethod(fake_send)}
                )(),
            },
        ):
            mgr.send_order('SA2609C2400', '0', 1, 1.0)
            mgr.send_order('SA2609C2500', '0', 1, 1.0)

        self.assertEqual(len(sent), 1)
        self.assertEqual(len(mgr.logger.errors), 2)


class TestInstallFailureSignals(unittest.TestCase):
    """守卫安装失败必须返回 False 并暴露原因，调用方据此决定是否拒绝启动。

    覆盖之前 silent return 的两个分支：
      - import auto_order_manager 失败
      - 模块缺少 OrderManager 类
    """

    def setUp(self):
        self._saved_aom = sys.modules.get('auto_order_manager')
        order_whitelist_guard._INSTALLED = False
        order_whitelist_guard._INSTALL_ERROR = None

    def tearDown(self):
        if self._saved_aom is not None:
            sys.modules['auto_order_manager'] = self._saved_aom
        else:
            sys.modules.pop('auto_order_manager', None)
        order_whitelist_guard._INSTALLED = False
        order_whitelist_guard._INSTALL_ERROR = None

    def test_returns_false_when_module_missing(self):
        sys.modules.pop('auto_order_manager', None)

        # 拦截 import：让 finder 找不到 auto_order_manager
        import builtins
        real_import = builtins.__import__

        def blocking_import(name, *a, **k):
            if name == 'auto_order_manager':
                raise ImportError('blocked for test')
            return real_import(name, *a, **k)

        builtins.__import__ = blocking_import
        try:
            ok = install_send_order_month_guard()
        finally:
            builtins.__import__ = real_import

        self.assertFalse(ok)
        self.assertFalse(order_whitelist_guard.is_installed())
        err = order_whitelist_guard.get_install_error()
        self.assertIsNotNone(err)
        self.assertIn('auto_order_manager', err)

    def test_returns_false_when_class_missing(self):
        import types as _types
        sys.modules['auto_order_manager'] = _types.SimpleNamespace()

        ok = install_send_order_month_guard()
        self.assertFalse(ok)
        self.assertFalse(order_whitelist_guard.is_installed())
        err = order_whitelist_guard.get_install_error()
        self.assertIsNotNone(err)
        self.assertIn('OrderManager', err)


if __name__ == '__main__':
    unittest.main()
