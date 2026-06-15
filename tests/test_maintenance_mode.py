"""maintenance_mode 单元测试。"""

import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from maintenance_mode import (
    is_maintenance_mode,
    should_block_trading,
    wrap_connection_cancel_guard,
)


class TestMaintenanceMode(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_config_flag(self):
        self.assertTrue(is_maintenance_mode({'maintenance_mode': True}))
        self.assertFalse(is_maintenance_mode({'maintenance_mode': False}))

    def test_flag_file(self):
        flag = os.path.join(self.tmp.name, 'maint.flag')
        cfg = {'maintenance_mode': False, 'maintenance_mode_file': flag}
        self.assertFalse(is_maintenance_mode(cfg))
        with open(flag, 'w', encoding='utf-8') as f:
            f.write('1\n')
        self.assertTrue(is_maintenance_mode(cfg))

    def test_should_block_when_maintenance(self):
        conn = MagicMock()
        conn.config = {'maintenance_mode': True}
        conn._runtime_state = {}
        self.assertTrue(should_block_trading(conn, conn.config))

    def test_should_not_block_during_shutdown(self):
        conn = MagicMock()
        conn.config = {'maintenance_mode': True}
        conn._runtime_state = {'_shutdown_cancel': True}
        self.assertFalse(should_block_trading(conn, conn.config))

    def test_cancel_guard_blocks_in_maintenance(self):
        conn = MagicMock()
        conn.config = {'maintenance_mode': True}
        conn._runtime_state = {}
        conn.logger = MagicMock()

        def _cancel(**kwargs):
            return 5

        conn.cancel_all_pending_orders = _cancel
        wrap_connection_cancel_guard(conn, conn.config)
        self.assertEqual(conn.cancel_all_pending_orders(), 0)

    def test_cancel_guard_wraps_each_connection(self):
        conn1 = MagicMock()
        conn1.config = {'maintenance_mode': True}
        conn1._runtime_state = {}
        conn1.logger = MagicMock()

        def _cancel1(**kwargs):
            return 3

        conn1.cancel_all_pending_orders = _cancel1

        conn2 = MagicMock()
        conn2.config = {'maintenance_mode': True}
        conn2._runtime_state = {}
        conn2.logger = MagicMock()

        def _cancel2(**kwargs):
            return 7

        conn2.cancel_all_pending_orders = _cancel2

        wrap_connection_cancel_guard(conn1, conn1.config)
        wrap_connection_cancel_guard(conn2, conn2.config)

        self.assertEqual(conn1.cancel_all_pending_orders(), 0)
        self.assertEqual(conn2.cancel_all_pending_orders(), 0)
        self.assertTrue(getattr(conn1, '_maintenance_cancel_wrapped', None) is True)
        self.assertTrue(getattr(conn2, '_maintenance_cancel_wrapped', None) is True)


if __name__ == '__main__':
    unittest.main()
