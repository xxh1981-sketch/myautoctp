"""ctp_recovery_patch 单元测试：helper + R3 真值表 + B5 重建。"""

import os
import sys
import types
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ctp_recovery_patch as crp


class _FakeLogger:
    def info(self, *a, **kw): pass
    def warning(self, *a, **kw): pass
    def error(self, *a, **kw): pass
    def debug(self, *a, **kw): pass


def _install_stub_arr(monkey_patch_quarantine: bool = True) -> types.ModuleType:
    """Provide a fake ``auto_reconnect_recovery`` module so patched body can
    import its three helpers and so we can observe ``set_quarantine`` calls.

    The patch installer skips when the module has no original
    ``run_post_reconnect_recovery`` to wrap, so we plant a placeholder.
    """
    arr = sys.modules.get('auto_reconnect_recovery')
    if arr is None or not isinstance(arr, types.ModuleType):
        arr = types.ModuleType('auto_reconnect_recovery')
        sys.modules['auto_reconnect_recovery'] = arr

    arr.run_post_reconnect_recovery = lambda conn, logger: None
    arr.both_channels_ready = lambda conn: bool(
        getattr(conn, 'td_logined', False)
        and getattr(conn, 'md_logined', False)
    )
    arr.wait_for_both_channels = lambda conn, logger, config: arr.both_channels_ready(conn)

    def _set_quarantine(conn, active):
        prev = bool(getattr(conn, '_reconnect_quarantine', False))
        conn._reconnect_quarantine = bool(active)
        if active and not prev:
            conn._quarantine_since = 1.0
        elif not active:
            conn._quarantine_since = 0.0

    arr.set_quarantine = _set_quarantine
    arr.check_quarantine_watchdog = getattr(
        arr, 'check_quarantine_watchdog', lambda *a, **kw: None,
    )
    return arr


def _build_conn(
    *,
    td_logined: bool = True,
    md_logined: bool = True,
    code_table_loaded: bool = True,
    cancel_side_effect=None,
    cancel_return: int = 0,
    query_orders=None,
):
    conn = MagicMock()
    conn.config = {
        'reconnect_code_table_wait': 0,
        'reconnect_both_login_wait': 0,
    }
    conn.code_table_loaded = code_table_loaded
    conn.td_logined = td_logined
    conn.md_logined = md_logined
    conn.td_api = MagicMock() if td_logined else None
    conn._reconnect_quarantine = True
    conn._quarantine_since = 1.0
    conn._reconnect_mgr = MagicMock()
    conn._reconnect_mgr.td_reconnect_count = 2
    conn._reconnect_mgr.md_reconnect_count = 1

    if cancel_side_effect is not None:
        conn.cancel_all_pending_orders = MagicMock(side_effect=cancel_side_effect)
    else:
        conn.cancel_all_pending_orders = MagicMock(return_value=cancel_return)

    conn.position_tracker = MagicMock()
    conn.position_tracker.calibrate_from_ctp.return_value = (True, 'ok')

    conn.query_orders_sync = MagicMock(return_value=query_orders or [])
    conn.lock = MagicMock()
    conn.lock.__enter__ = MagicMock(return_value=None)
    conn.lock.__exit__ = MagicMock(return_value=False)
    conn.pending_orders = {}
    conn.order_traded = {}
    return conn


def _install_patched() -> types.ModuleType:
    arr = _install_stub_arr()
    crp._INSTALLED = False
    crp.install_recovery_patch()
    return arr


class TestRecoveryPatchHelpers(unittest.TestCase):

    def test_pending_status_set(self):
        self.assertTrue(crp._is_pending_status('1'))
        self.assertTrue(crp._is_pending_status('3'))
        self.assertTrue(crp._is_pending_status('a'))
        self.assertFalse(crp._is_pending_status('0'))
        self.assertFalse(crp._is_pending_status('5'))

    def test_install_idempotent(self):
        _install_stub_arr()
        crp._INSTALLED = False
        crp.install_recovery_patch()
        state_after_first = crp._INSTALLED
        crp.install_recovery_patch()
        self.assertEqual(crp._INSTALLED, state_after_first)


class TestPatchedRecoveryOk(unittest.TestCase):
    """R3 真值表：``recovery_ok`` 必须同时满足
    ``not skip_cancel`` 与 ``cancel_attempted_ok`` 才解除隔离期。"""

    def setUp(self) -> None:
        self.arr = _install_patched()

    # ---- 仅当撤单完整成功 + 双通道登录 → 解除隔离 ----

    def test_cancel_ok_clears_quarantine(self):
        conn = _build_conn(code_table_loaded=True, cancel_return=0)
        self.arr.run_post_reconnect_recovery(conn, _FakeLogger())
        self.assertFalse(conn._reconnect_quarantine)
        self.assertEqual(conn._reconnect_mgr.td_reconnect_count, 0)
        self.assertEqual(conn._reconnect_mgr.md_reconnect_count, 0)

    # ---- 撤单抛异常：核心新增覆盖（旧实现会错误清隔离）----

    def test_cancel_raises_keeps_quarantine(self):
        conn = _build_conn(
            code_table_loaded=True,
            cancel_side_effect=RuntimeError('broker rate-limit'),
        )
        self.arr.run_post_reconnect_recovery(conn, _FakeLogger())
        self.assertTrue(
            conn._reconnect_quarantine,
            '撤单抛异常时必须保留隔离期，避免本地清空 pending 但交易所仍有挂单',
        )
        self.assertEqual(conn._reconnect_mgr.td_reconnect_count, 2)
        self.assertEqual(conn._reconnect_mgr.md_reconnect_count, 1)

    # ---- 码表未就绪：跳过撤单，必须保留隔离 ----

    def test_code_table_timeout_keeps_quarantine(self):
        conn = _build_conn(code_table_loaded=False)
        conn.config['reconnect_code_table_wait'] = 0
        self.arr.run_post_reconnect_recovery(conn, _FakeLogger())
        self.assertTrue(conn._reconnect_quarantine)
        conn.cancel_all_pending_orders.assert_not_called()

    # ---- 双通道未就绪：直接 return，保持隔离 ----

    def test_both_channels_not_ready_keeps_quarantine(self):
        conn = _build_conn(md_logined=False)
        self.arr.run_post_reconnect_recovery(conn, _FakeLogger())
        self.assertTrue(conn._reconnect_quarantine)
        conn.cancel_all_pending_orders.assert_not_called()

    # ---- 校准失败不阻断（有意设计：下次周期重试）----

    def test_calibration_failure_does_not_block_clear(self):
        conn = _build_conn(cancel_return=0)
        conn.position_tracker.calibrate_from_ctp.return_value = (False, 'timeout')
        self.arr.run_post_reconnect_recovery(conn, _FakeLogger())
        self.assertFalse(conn._reconnect_quarantine)

    # ---- B5 异常不阻断解除隔离（仅日志 warning）----

    def test_b5_rebuild_exception_does_not_block(self):
        conn = _build_conn(cancel_return=0)
        conn.query_orders_sync = MagicMock(side_effect=RuntimeError('rpc'))
        self.arr.run_post_reconnect_recovery(conn, _FakeLogger())
        self.assertFalse(conn._reconnect_quarantine)

    # ---- H4: 撤单成功但交易所仍有非终结挂单 → 保持隔离 ----

    def test_exchange_still_pending_keeps_quarantine(self):
        conn = _build_conn(
            cancel_return=1,
            query_orders=[{
                'status': '3',  # NO_TRADE_QUEUEING，非终结
                'order_ref': 1234,
                'volume_total': 1,
                'price': 1.0,
                'instrument': 'rb2401C4000',
                'exchange_id': 'SHFE',
                'direction': '0',
                'offset': '0',
            }],
        )
        self.arr.run_post_reconnect_recovery(conn, _FakeLogger())
        self.assertTrue(
            conn._reconnect_quarantine,
            '撤单后交易所仍有非终结挂单时必须保留隔离期',
        )

    # ---- H4: 撤单成功且交易所仅余终结态订单 → 正常解除隔离 ----

    def test_exchange_only_terminal_clears_quarantine(self):
        conn = _build_conn(
            cancel_return=1,
            query_orders=[{
                'status': '5',  # CANCELED，终结态，不计入 pending
                'order_ref': 1234,
                'instrument': 'rb2401C4000',
                'exchange_id': 'SHFE',
            }],
        )
        self.arr.run_post_reconnect_recovery(conn, _FakeLogger())
        self.assertFalse(conn._reconnect_quarantine)


if __name__ == '__main__':
    unittest.main()
