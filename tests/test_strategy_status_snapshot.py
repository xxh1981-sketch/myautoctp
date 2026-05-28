"""strategy_status_snapshot 三态与飞书格式。"""

import unittest
from unittest.mock import MagicMock

from strategy_status_snapshot import (
    STATUS_OPEN,
    STATUS_OTHER,
    _resolve_spread_status,
    format_strategy_status_message,
)


class TestStrategyStatusSnapshot(unittest.TestCase):
    def test_format_message_three_states(self):
        snap = {
            "ts": 1_700_000_000.0,
            "summary": {
                "spread_current_groups": 2,
                "spread_target_groups": 8,
                "strangle_current_groups": 3,
                "strangle_target_groups": 8,
            },
            "spread": {
                "by_symbol": [{
                    "symbol": "AG",
                    "month": "2608",
                    "current_groups": 2,
                    "target_groups": 8,
                    "status": STATUS_OTHER,
                    "status_reason": "VIX未达开仓",
                    "gates": [{"name": "daily_limit", "ok": True, "detail": ""}],
                    "meta": {"position_detail": "A=2/8, B=4/16"},
                }],
            },
            "strangle": {
                "by_symbol": [{
                    "symbol": "AG",
                    "month": "2608",
                    "current_groups": 3,
                    "target_groups": 8,
                    "status": STATUS_OPEN,
                    "status_reason": "VIX偏低可建仓",
                    "gates": [{"name": "buy_limit", "ok": True, "detail": ""}],
                    "meta": {"unmatched_open": 0, "unmatched_close": 0},
                }],
            },
        }
        text = format_strategy_status_message(snap)
        self.assertIn("状态: 其他", text)
        self.assertIn("状态: 开仓", text)
        self.assertIn("门闸:", text)
        self.assertNotIn("开仓✅", text)

    def test_spread_feishu_pause_is_other(self):
        conn = MagicMock()
        conn._reconnect_quarantine = False
        conn.td_logined = True
        conn.md_logined = True
        conn._runtime_state = {}
        conn.futures_prices = {"ag": 8000.0}
        conn.query_positions_sync.return_value = []

        item = {
            "future": "AG",
            "month": "2608",
            "vol_basis": 0.2,
            "vol_of_combo": 8,
        }
        status, reason = _resolve_spread_status(
            conn,
            item,
            {},
            [],
            "",
            None,
            False,
            False,
            False,
            "",
            True,
            0,
            100,
            True,
            MagicMock(),
        )
        self.assertEqual(status, STATUS_OTHER)
        self.assertIn("飞书", reason)


if __name__ == "__main__":
    unittest.main()
