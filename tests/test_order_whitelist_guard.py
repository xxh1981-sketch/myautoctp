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
    _canonical_send_instrument,
    _is_option_like,
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
        self.assertTrue(any('非期权' in e or '期货' in e for e in mgr.logger.errors))

    def test_rejects_cp_product_futures(self):
        """品种代码本身是 C/P 的期货不得因启发式误放行。"""
        cases = [
            ('p', '2705', 'P2705'),
            ('p', '2701', 'P2701'),
            ('c', '2701', 'C2701'),
            ('c', '2701', 'c2701'),
        ]
        for sym, month, fut in cases:
            with self.subTest(fut=fut):
                self._delegated_calls.clear()
                conn = FakeConn([sym], {sym: [month]})
                mgr = self._make_mgr(conn)
                ref, _ = mgr.send_order(fut, '0', 1, 1.0)
                self.assertIsNone(ref)
                self.assertEqual(self._delegated_calls, [])
                self.assertTrue(
                    any('非期权' in e or '期货' in e for e in mgr.logger.errors),
                    mgr.logger.errors,
                )

    def test_accepts_cp_product_options(self):
        """C/P 品种的真实期权仍可发单；大商所前缀按交易所规则改小写。"""
        conn = FakeConn(['c', 'p'], {'c': ['2701'], 'p': ['2701']})
        mgr = self._make_mgr(conn)
        cases = (
            ('C2701-C-2340', 'c2701-C-2340'),
            ('C2701-P-2140', 'c2701-P-2140'),
            ('P2701-C-8800', 'p2701-C-8800'),
            ('p2701-P-7800', 'p2701-P-7800'),
        )
        for opt, wire in cases:
            with self.subTest(opt=opt):
                self._delegated_calls.clear()
                ref, _ = mgr.send_order(opt, '0', 1, 1.0)
                self.assertEqual(ref, 999)
                self.assertEqual(self._delegated_calls, [wire])

    def test_remaps_dce_case_to_quotes_key(self):
        """CSV 大写 C2701-C-2340 须按行情键改成大商所小写再发单。"""
        conn = FakeConn(['c'], {'c': ['2701']})
        conn.quotes = {'c2701-C-2340': object()}
        mgr = self._make_mgr(conn)
        ref, _ = mgr.send_order('C2701-C-2340', '0', 1, 27.5)
        self.assertEqual(ref, 999)
        self.assertEqual(self._delegated_calls, ['c2701-C-2340'])

    def test_rejects_unparsable_month(self):
        """合约月份无法解析（前缀后无数字段）→ 拒绝。"""
        conn = FakeConn(['abc'], {'abc': ['2608']})
        mgr = self._make_mgr(conn)
        # 有 -[CP]- 行权价段，但无 ^[a-z]+\d{3,4} 月份段。
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


class TestCanonicalSendInstrument(unittest.TestCase):
    def test_quotes_key_wins(self):
        conn = FakeConn(['c'], {'c': ['2701']})
        conn.quotes = {'c2701-C-2340': object()}
        self.assertEqual(
            _canonical_send_instrument(conn, 'C2701-C-2340'),
            'c2701-C-2340',
        )

    def test_option_info_fallback(self):
        conn = FakeConn(['si'], {'si': ['2611']})
        conn.option_info = {'si': {'si2611-C-9400': {'product_class': '2'}}}
        self.assertEqual(
            _canonical_send_instrument(conn, 'SI2611-C-9400'),
            'si2611-C-9400',
        )

    def test_heuristic_when_no_index(self):
        conn = FakeConn(['c'], {'c': ['2701']})
        self.assertEqual(
            _canonical_send_instrument(conn, 'C2701-C-2340'),
            'c2701-C-2340',
        )


class TestIsOptionLike(unittest.TestCase):
    def test_futures_rejected(self):
        for fut in ('SA2608', 'P2705', 'C2701', 'c2701', 'm2701', 'IF2606', 'ag2606'):
            self.assertFalse(_is_option_like(fut), fut)

    def test_options_accepted(self):
        for opt in (
            'SA2608C2400', 'IO2604-C-4000', 'C2701-C-2340', 'C2701-P-2140',
            'P2705-C-8800', 'm2701-P-2900', 'c2701-MS-C-2320', 'RM509-C-9000',
        ):
            self.assertTrue(_is_option_like(opt), opt)

    def test_empty_rejected(self):
        self.assertFalse(_is_option_like(''))
        self.assertFalse(_is_option_like(None))


if __name__ == '__main__':
    unittest.main()
