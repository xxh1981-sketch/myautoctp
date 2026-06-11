"""feishu_alert_cooldown unit tests"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import autotrade_stubs

autotrade_stubs.ensure_auto_feishu_stub()

from feishu_alert_cooldown import (
    install_send_feishu_throttle,
    send_message_cooldown,
    should_send,
    throttle_key_for_message,
)


class TestThrottleKey(unittest.TestCase):

    def test_open_liquidity_key(self):
        msg = '[SA] 流动性检查未通过，跳过交易\n  bid/ask'
        self.assertEqual(throttle_key_for_message(msg), 'spread_open_liq:sa')

    def test_fill_report_not_throttled(self):
        msg = '✅ **Fill Report / 成交回报**\n\n**Instrument** SA'
        self.assertIsNone(throttle_key_for_message(msg))

    def test_strangle_reject_key(self):
        msg = (
            '❌ **宽跨交易所拒单**\n\n'
            '**品种**: SA\n**合约**: SA609C1000\n**Ref**: 500001'
        )
        self.assertEqual(
            throttle_key_for_message(msg),
            'strangle_reject:sa:SA609C1000',
        )


class TestShouldSend(unittest.TestCase):

    def test_cooldown_blocks_repeat(self):
        conn = MagicMock()
        conn._runtime_state = {}
        cfg = {'feishu_alert_cooldown_sec': 300}
        self.assertTrue(
            should_send('k1', conn=conn, config=cfg),
        )
        self.assertFalse(
            should_send('k1', conn=conn, config=cfg),
        )


class TestInstallThrottle(unittest.TestCase):

    def _reset_patch(self):
        import feishu_alert_cooldown as mod
        import auto_feishu
        mod._PATCHED = False
        if mod._ORIG_SEND is not None:
            auto_feishu.send_feishu_message = mod._ORIG_SEND
            mod._ORIG_SEND = None

    def setUp(self):
        self._reset_patch()

    def tearDown(self):
        self._reset_patch()

    @patch('auto_feishu.send_feishu_message', return_value=True)
    def test_throttles_repeated_liquidity(self, mock_send):
        import auto_feishu
        install_send_feishu_throttle({'feishu_alert_cooldown_sec': 300})
        conn = MagicMock()
        conn._runtime_state = {}
        cfg = {'_spread_fill_conn': conn, 'feishu_alert_cooldown_sec': 300}
        msg = '[MA] 流动性检查未通过，跳过交易'
        auto_feishu.send_feishu_message(msg, config=cfg)
        auto_feishu.send_feishu_message(msg, config=cfg)
        self.assertEqual(mock_send.call_count, 1)

    @patch('auto_feishu.send_feishu_message', return_value=True)
    def test_fill_report_always_sent(self, mock_send):
        import auto_feishu
        install_send_feishu_throttle({})
        msg = '✅ **Fill Report / 成交回报**\n\n**Instrument** X'
        auto_feishu.send_feishu_message(msg, config={})
        auto_feishu.send_feishu_message(msg, config={})
        self.assertEqual(mock_send.call_count, 2)


class TestSendMessageCooldown(unittest.TestCase):

    @patch('auto_feishu.send_feishu_message', return_value=True)
    def test_explicit_helper(self, mock_send):
        conn = MagicMock()
        conn._runtime_state = {}
        cfg = {'feishu_alert_cooldown_sec': 60}
        ok1 = send_message_cooldown(
            'test', alert_key='x', config=cfg, conn=conn,
        )
        ok2 = send_message_cooldown(
            'test', alert_key='x', config=cfg, conn=conn,
        )
        self.assertTrue(ok1)
        self.assertFalse(ok2)
        mock_send.assert_called_once()


if __name__ == '__main__':
    unittest.main()
