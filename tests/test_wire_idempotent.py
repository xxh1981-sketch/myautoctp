"""Wire-* idempotency: repeated wire calls must not grow handler chain."""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ctp_bootstrap  # noqa: F401


class TestWireIdempotent(unittest.TestCase):

    def _make_conn(self):
        conn = MagicMock()
        conn._runtime_state = {}
        return conn

    def _table_size(self, conn):
        return len(conn._runtime_state.get('_wire_handler_table') or {})

    def test_strangle_wire_idempotent(self):
        from strangle_fill_sync import wire_strangle_trade_runtime

        conn = self._make_conn()
        ledger = MagicMock()
        wire_strangle_trade_runtime(conn, ledger)
        wire_strangle_trade_runtime(conn, ledger)
        wire_strangle_trade_runtime(conn, ledger)
        self.assertEqual(
            self._table_size(conn), 1,
            '同 kind 重复 wire 应仅占 dispatch 表的 1 个槽位',
        )

    def test_spread_wire_idempotent(self):
        from spread_fill_sync import wire_spread_trade_runtime

        conn = self._make_conn()
        store = MagicMock()
        wire_spread_trade_runtime(conn, store)
        wire_spread_trade_runtime(conn, store)
        self.assertEqual(self._table_size(conn), 1)

    def test_fill_ledger_wire_idempotent(self):
        from fill_ledger import wire_fill_ledger

        conn = self._make_conn()
        wire_fill_ledger(conn)
        wire_fill_ledger(conn)
        wire_fill_ledger(conn)
        self.assertEqual(self._table_size(conn), 1)

    def test_full_chain_no_duplication_on_rewire(self):
        """模拟启动序列 + 重连场景：三次 wire（按顺序）后再重复 wire 一遍，
        dispatch 表必须仍是 3 个槽位，不能膨胀。"""
        from strangle_fill_sync import wire_strangle_trade_runtime
        from spread_fill_sync import wire_spread_trade_runtime
        from fill_ledger import wire_fill_ledger

        conn = self._make_conn()
        ledger = MagicMock()
        store = MagicMock()

        wire_strangle_trade_runtime(conn, ledger)
        wire_spread_trade_runtime(conn, store)
        wire_fill_ledger(conn)
        self.assertEqual(self._table_size(conn), 3)

        wire_strangle_trade_runtime(conn, ledger)
        wire_spread_trade_runtime(conn, store)
        wire_fill_ledger(conn)
        self.assertEqual(
            self._table_size(conn), 3,
            '重复 wire 不能让 dispatch 表膨胀',
        )

    def test_handler_still_dispatches_each_kind_once(self):
        """重复 wire 后，每种 handler 仍应被调用恰好一次。"""
        from strangle_fill_sync import wire_strangle_trade_runtime
        from spread_fill_sync import wire_spread_trade_runtime
        from fill_ledger import wire_fill_ledger

        conn = self._make_conn()
        ledger = MagicMock()
        store = MagicMock()

        wire_strangle_trade_runtime(conn, ledger)
        wire_spread_trade_runtime(conn, store)
        wire_fill_ledger(conn)
        # 重复一次
        wire_strangle_trade_runtime(conn, ledger)
        wire_spread_trade_runtime(conn, store)
        wire_fill_ledger(conn)

        with patch('strangle_fill_sync.handle_strangle_trade_rtn') as ms, \
             patch('spread_fill_sync.handle_spread_trade_rtn') as msp, \
             patch('fill_ledger.handle_fill_rtn') as mf:
            handler = conn._runtime_state['_strangle_trade_handler']
            p_trade = MagicMock()
            handler(conn, p_trade, MagicMock())
            self.assertEqual(ms.call_count, 1)
            self.assertEqual(msp.call_count, 1)
            self.assertEqual(mf.call_count, 1)


if __name__ == '__main__':
    unittest.main()
