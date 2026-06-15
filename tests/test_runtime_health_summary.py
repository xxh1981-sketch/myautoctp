"""runtime_health_summary 单元测试。"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from runtime_health_summary import (
    build_runtime_health_summary,
    format_runtime_health_heartbeat_lines,
    format_runtime_health_text,
)


def _conn(**kwargs):
    conn = MagicMock()
    conn._runtime_state = kwargs.get('runtime', {})
    conn._reconnect_quarantine = kwargs.get('quarantine', False)
    conn.td_logined = kwargs.get('td', True)
    conn.md_logined = kwargs.get('md', True)
    return conn


class TestRuntimeHealthSummary(unittest.TestCase):

    @patch('runtime_health_summary._resolve_feishu_paused', return_value=False)
    @patch('runtime_health_summary.is_maintenance_mode', return_value=False)
    def test_can_auto_trade_when_healthy(self, _maint, _pause):
        conn = _conn()
        summary = build_runtime_health_summary(
            conn,
            {'daily_trade_limit': 100, '_process_started_at': 1000.0},
            feishu_paused=False,
        )
        self.assertTrue(summary['can_auto_trade'])
        self.assertTrue(summary['can_auto_open'])

    @patch('runtime_health_summary._resolve_feishu_paused', return_value=True)
    @patch('runtime_health_summary.is_maintenance_mode', return_value=False)
    def test_feishu_pause_blocks_auto_trade(self, _maint, _pause):
        conn = _conn()
        summary = build_runtime_health_summary(conn, {}, feishu_paused=True)
        self.assertFalse(summary['can_auto_trade'])
        self.assertIn('飞书暂停', summary['block_reasons'])

    @patch('runtime_health_summary._resolve_feishu_paused', return_value=False)
    @patch('runtime_health_summary.is_maintenance_mode', return_value=True)
    def test_maintenance_blocks_auto_trade(self, _maint, _pause):
        conn = _conn()
        summary = build_runtime_health_summary(conn, {}, feishu_paused=False)
        self.assertFalse(summary['can_auto_trade'])
        self.assertIn('维护模式', summary['block_reasons'])

    @patch('runtime_health_summary._resolve_feishu_paused', return_value=False)
    @patch('runtime_health_summary.is_maintenance_mode', return_value=False)
    def test_reconcile_halt_blocks_open_only(self, _maint, _pause):
        conn = _conn(runtime={'_spread_reconcile_halt': True})
        summary = build_runtime_health_summary(conn, {}, feishu_paused=False)
        self.assertTrue(summary['can_auto_trade'])
        self.assertFalse(summary['can_auto_open'])

    def test_heartbeat_lines_include_flags(self):
        summary = {
            'can_auto_trade': True,
            'can_auto_open': False,
            'feishu_paused': False,
            'maintenance_mode': True,
            'halt_margin': True,
            'halt_journal': False,
            'halt_spread_reconcile': False,
            'halt_strangle_reconcile': False,
            'halt_position_csv': True,
            'loop_errors': 2,
            'slow_rounds': 1,
            'last_round_sec': 12.5,
            'spread_filled': 3,
            'spread_daily_limit': 100,
            'open_unmatched': 0,
        }
        lines = format_runtime_health_heartbeat_lines(summary)
        joined = '\n'.join(lines)
        self.assertIn('maintenance_mode=1', joined)
        self.assertIn('halt_position_csv=1', joined)
        self.assertIn('last_round_sec=12.5', joined)

    def test_format_text_includes_tradable_state(self):
        text = format_runtime_health_text({
            'can_auto_trade': False,
            'can_auto_open': False,
            'block_reasons': ['飞书暂停'],
            'feishu_paused': True,
            'maintenance_mode': False,
            'quarantine': False,
            'halt_margin': False,
            'halt_journal': False,
            'halt_spread_reconcile': False,
            'halt_strangle_reconcile': False,
            'halt_position_csv': False,
            'loop_errors': 0,
        })
        self.assertIn('不可自动交易', text)
        self.assertIn('飞书暂停', text)


if __name__ == '__main__':
    unittest.main()
