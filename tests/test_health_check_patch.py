"""health_check_patch 辅助逻辑单元测试。"""

import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import health_check_patch as hcp


class TestHealthCheckPatchHelpers(unittest.TestCase):

    def test_exchange_pending_refs_filters_terminal(self):
        conn = MagicMock()
        conn.query_orders_sync.return_value = [
            {'order_ref': 100, 'status': '3'},
            {'order_ref': 200, 'status': '5'},
            {'order_ref': 'bad', 'status': '1'},
            {'order_ref': 0, 'status': '1'},
        ]
        refs = hcp._exchange_pending_refs(conn, logger=None)
        self.assertEqual(refs, {100})

    def test_exchange_pending_refs_query_failure_returns_empty(self):
        conn = MagicMock()
        conn.query_orders_sync.side_effect = RuntimeError('timeout')
        refs = hcp._exchange_pending_refs(conn, logger=MagicMock())
        self.assertEqual(refs, set())

    def test_conf_reads_from_conn_config(self):
        conn = MagicMock()
        conn.config = {'zombie_cancel_cooldown_sec': 42}
        self.assertEqual(hcp._conf(conn, 'zombie_cancel_cooldown_sec', 300), 42)
        self.assertEqual(hcp._conf(conn, 'missing', 300), 300)

    def test_install_idempotent(self):
        hcp._INSTALLED = False
        hcp.install_health_check_patch()
        state_after_first = hcp._INSTALLED
        hcp.install_health_check_patch()
        self.assertEqual(hcp._INSTALLED, state_after_first)


if __name__ == '__main__':
    unittest.main()
