"""ctp_heartbeat_guard 单元测试（不依赖 autotrade SPI 安装）。"""

import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ctp_heartbeat_guard as hg


class TestHeartbeatGuardLogic(unittest.TestCase):

    def _conn(self, **cfg):
        conn = MagicMock()
        conn.config = cfg
        conn._runtime_state = {}
        conn.logger = MagicMock()
        return conn

    def test_ensure_state_creates_bucket(self):
        conn = self._conn()
        state = hg._ensure_state(conn)
        self.assertIn('td_last_warn_ts', state)
        self.assertIs(conn._runtime_state['_heartbeat_state'], state)

    def test_warn_below_dead_threshold_no_mark_dead(self):
        conn = self._conn(ctp_heartbeat_dead_threshold_sec=90)
        spi = MagicMock()
        spi.conn = conn
        mgr = MagicMock()
        conn._reconnect_mgr = mgr

        hg._on_heartbeat_warning(spi, 30, channel='td')

        mgr.mark_connection_dead.assert_not_called()
        conn.logger.warning.assert_called()

    def test_exceed_dead_threshold_marks_td_dead(self):
        conn = self._conn(
            ctp_heartbeat_dead_threshold_sec=90,
            ctp_heartbeat_dead_cooldown_sec=0,
            ctp_heartbeat_log_cooldown_sec=0,
        )
        spi = MagicMock()
        spi.conn = conn
        mgr = MagicMock()
        conn._reconnect_mgr = mgr

        hg._on_heartbeat_warning(spi, 120, channel='td')

        mgr.mark_connection_dead.assert_called_once()

    def test_md_channel_triggers_on_front_disconnected(self):
        conn = self._conn(
            ctp_heartbeat_dead_threshold_sec=90,
            ctp_heartbeat_dead_cooldown_sec=0,
            ctp_heartbeat_log_cooldown_sec=0,
        )
        spi = MagicMock()
        spi.conn = conn

        hg._on_heartbeat_warning(spi, 100, channel='md')

        spi.OnFrontDisconnected.assert_called_once_with(-9)

    def test_install_idempotent(self):
        hg._INSTALLED = False
        hg.install_heartbeat_warning()
        state_after_first = hg._INSTALLED
        hg.install_heartbeat_warning()
        self.assertEqual(hg._INSTALLED, state_after_first)


if __name__ == '__main__':
    unittest.main()
