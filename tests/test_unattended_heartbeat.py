"""unattended_heartbeat unit tests（心跳文件 + 每日飞书报平安 + 重启原因）"""

import os
import sys
import unittest
from datetime import date
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import autotrade_stubs

autotrade_stubs.ensure_auto_feishu_stub()

import tempfile

import unattended_heartbeat
from unattended_heartbeat import (
    maybe_send_daily_heartbeat,
    read_restart_reason_summary,
    touch_heartbeat_file,
    write_restart_reason,
    write_stopped_heartbeat,
)


def _conn():
    conn = MagicMock()
    conn._runtime_state = {}
    conn._reconnect_quarantine = False
    return conn


class TestTouchHeartbeatFile(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.hb = os.path.join(self.tmp.name, 'hb.txt')

    def test_writes_running_status_and_pid(self):
        conn = _conn()
        conn._runtime_state['_margin_halt_open'] = True
        cfg = {'heartbeat_file': self.hb, '_process_started_at': 1000.0}
        with patch('unattended_heartbeat.time.time', return_value=2000.0):
            self.assertTrue(
                touch_heartbeat_file(
                    cfg, logger=None, conn=conn, process_started_at=1000.0,
                ),
            )
        with open(self.hb, 'r', encoding='utf-8') as f:
            content = f.read()
        self.assertIn(f'pid={os.getpid()}', content)
        self.assertIn('status=RUNNING', content)
        self.assertIn('uptime_sec=1000', content)
        self.assertIn('halt_margin=1', content)

    def test_disabled_when_empty_path(self):
        self.assertFalse(touch_heartbeat_file({'heartbeat_file': ''}, None))
        self.assertFalse(touch_heartbeat_file({}, None))

    def test_repo_data_path_blocked_under_pytest(self):
        from data_path_guard import repo_data_dir
        cfg = {'heartbeat_file': os.path.join(repo_data_dir(), 'hb.txt')}
        self.assertFalse(touch_heartbeat_file(cfg, None))
        self.assertFalse(os.path.exists(cfg['heartbeat_file']))

    def test_failure_does_not_raise(self):
        blocker = os.path.join(self.tmp.name, 'blocker')
        with open(blocker, 'w', encoding='utf-8') as f:
            f.write('x')
        cfg = {'heartbeat_file': os.path.join(blocker, 'hb.txt')}
        logger = MagicMock()
        self.assertFalse(touch_heartbeat_file(cfg, logger))


class TestStoppedAndRestartReason(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.hb = os.path.join(self.tmp.name, 'hb.txt')
        self.reason = os.path.join(self.tmp.name, 'reason.txt')

    def test_write_stopped_heartbeat(self):
        cfg = {'heartbeat_file': self.hb}
        self.assertTrue(write_stopped_heartbeat(cfg, reason='SIGTERM'))
        with open(self.hb, 'r', encoding='utf-8') as f:
            content = f.read()
        self.assertIn('status=STOPPED', content)
        self.assertIn('reason=SIGTERM', content)

    def test_write_and_read_restart_reason(self):
        cfg = {'restart_reason_file': self.reason}
        self.assertTrue(
            write_restart_reason(cfg, 'CRASH_RESTART', detail='boom'),
        )
        self.assertEqual(read_restart_reason_summary(cfg), 'CRASH_RESTART')
        with open(self.reason, 'r', encoding='utf-8') as f:
            self.assertIn('detail=boom', f.read())


class TestDailyHeartbeat(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.marker = os.path.join(self.tmp.name, 'sent.txt')
        self.reason = os.path.join(self.tmp.name, 'reason.txt')
        self.cfg = {
            'daily_heartbeat_enabled': True,
            'daily_heartbeat_hour': 0,
            'daily_heartbeat_marker_file': self.marker,
            'restart_reason_file': self.reason,
        }

    def test_sends_once_and_writes_marker(self):
        conn = _conn()
        logger = MagicMock()
        with patch('auto_feishu.send_feishu_message', return_value=True) as m:
            self.assertTrue(maybe_send_daily_heartbeat(conn, self.cfg, logger))
        m.assert_called_once()
        self.assertIn('报平安', m.call_args.args[0])
        with open(self.marker, 'r', encoding='utf-8') as f:
            self.assertEqual(f.read().strip(), date.today().isoformat())
        with patch('auto_feishu.send_feishu_message', return_value=True) as m2:
            self.assertFalse(maybe_send_daily_heartbeat(conn, self.cfg, logger))
        m2.assert_not_called()

    def test_no_marker_on_send_failure_and_retry_throttled(self):
        conn = _conn()
        logger = MagicMock()
        with patch('auto_feishu.send_feishu_message', return_value=False) as m:
            self.assertFalse(maybe_send_daily_heartbeat(conn, self.cfg, logger))
        m.assert_called_once()
        self.assertFalse(os.path.exists(self.marker))
        with patch('auto_feishu.send_feishu_message', return_value=True) as m2:
            self.assertFalse(maybe_send_daily_heartbeat(conn, self.cfg, logger))
        m2.assert_not_called()
        conn._runtime_state['_daily_heartbeat_last_attempt'] = 0.0
        with patch('auto_feishu.send_feishu_message', return_value=True) as m3:
            self.assertTrue(maybe_send_daily_heartbeat(conn, self.cfg, logger))
        m3.assert_called_once()

    def test_send_exception_does_not_raise(self):
        conn = _conn()
        logger = MagicMock()
        with patch(
            'auto_feishu.send_feishu_message', side_effect=RuntimeError('net'),
        ):
            self.assertFalse(maybe_send_daily_heartbeat(conn, self.cfg, logger))
        self.assertFalse(os.path.exists(self.marker))
        logger.warning.assert_called()

    def test_disabled_or_before_hour(self):
        conn = _conn()
        cfg = dict(self.cfg)
        cfg['daily_heartbeat_enabled'] = False
        with patch('auto_feishu.send_feishu_message', return_value=True) as m:
            self.assertFalse(maybe_send_daily_heartbeat(conn, cfg, None))
        m.assert_not_called()

        cfg = dict(self.cfg)
        cfg['daily_heartbeat_hour'] = 24
        with patch('auto_feishu.send_feishu_message', return_value=True) as m2:
            self.assertFalse(maybe_send_daily_heartbeat(conn, cfg, None))
        m2.assert_not_called()

    def test_repo_data_marker_blocked_under_pytest(self):
        from data_path_guard import repo_data_dir
        conn = _conn()
        cfg = dict(self.cfg)
        cfg['daily_heartbeat_marker_file'] = os.path.join(
            repo_data_dir(), 'sent.txt',
        )
        with patch('auto_feishu.send_feishu_message', return_value=True) as m:
            self.assertFalse(maybe_send_daily_heartbeat(conn, cfg, None))
        m.assert_not_called()

    def test_status_summary_reflects_halts(self):
        conn = _conn()
        conn._runtime_state['_journal_halt_open'] = True
        summary = unattended_heartbeat._status_summary(
            conn, config={'daily_trade_limit': 100},
        )
        self.assertIn('journal_halt=是', summary)
        self.assertIn('margin_halt=否', summary)

    def test_status_summary_includes_restart_reason(self):
        conn = _conn()
        write_restart_reason(self.cfg, 'CRASH_RESTART')
        summary = unattended_heartbeat._status_summary(conn, config=self.cfg)
        self.assertIn('上次启动原因: CRASH_RESTART', summary)

    def test_status_summary_includes_stashed_metrics(self):
        conn = _conn()
        conn._runtime_state['_daily_heartbeat_metrics'] = {
            'spread_filled': 3,
            'spread_daily_limit': 100,
            'strangle_buy_spent': 12000.0,
            'strangle_buy_limit': 300000.0,
            'margin_used': 45000.0,
            'margin_limit': 100000.0,
            'open_unmatched': 0,
        }
        summary = unattended_heartbeat._status_summary(conn)
        self.assertIn('价差成交 3/100', summary)
        self.assertIn('宽跨买入 12000/300000', summary)
        self.assertIn('保证金占用 45000/100000', summary)


if __name__ == '__main__':
    unittest.main()
