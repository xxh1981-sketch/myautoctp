"""trade_feishu_notify unit tests"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ctp_bootstrap  # noqa: F401

import autotrade_stubs

autotrade_stubs.ensure_auto_feishu_stub()

from trade_feishu_notify import (
    format_fill_feishu_message,
    install_unified_trade_feishu,
    notify_fill_trade,
    suppress_legacy_fill_feishu,
)


class TestFormatFillFeishu(unittest.TestCase):

    def test_full_message(self):
        row = {
            'instrument_code': 'SA609C1000',
            'fill_price': '50.2500',
            'bid_price': '50.2000',
            'ask_price': '50.3000',
            'slippage_vs_mid': '0.0500',
            'fill_volume': 2,
            'fill_side': 'buy_open',
            'strategy': 'strangle',
        }
        trade = {'order_ref': 500001, 'trade_date': '20260520', 'trade_time': '10:15:32'}
        msg = format_fill_feishu_message(row, trade)
        self.assertIn('Fill Report', msg)
        self.assertIn('SA609C1000', msg)
        self.assertIn('Buy Open', msg)
        self.assertIn('Strangle', msg)
        self.assertIn('50.2500', msg)
        self.assertIn('50.2000 / 50.3000', msg)
        self.assertIn('+0.0500 vs mid', msg)
        self.assertIn('500001', msg)
        self.assertIn('2026-05-20 10:15:32', msg)

    def test_no_quote(self):
        row = {
            'instrument_code': 'IO2604-C-4000',
            'fill_price': '12.5000',
            'bid_price': '',
            'ask_price': '',
            'slippage_vs_mid': '',
            'fill_volume': 1,
            'fill_side': 'sell_close',
            'strategy': 'spread',
        }
        msg = format_fill_feishu_message(row, {})
        self.assertIn('Sell Close', msg)
        self.assertIn('Spread', msg)
        self.assertIn('—', msg)


class TestNotifyFillTrade(unittest.TestCase):

    @patch('auto_feishu.send_feishu_message', return_value=True)
    def test_sends_when_enabled(self, mock_send):
        cfg = {'dual_strategy': {'fill_feishu_enabled': True}}
        row = {
            'instrument_code': 'X',
            'fill_price': '1.0000',
            'bid_price': '',
            'ask_price': '',
            'slippage_vs_mid': '',
            'fill_volume': 1,
            'fill_side': 'buy_open',
            'strategy': 'spread',
        }
        ok = notify_fill_trade(MagicMock(), {}, row, cfg)
        self.assertTrue(ok)
        mock_send.assert_called_once()
        self.assertIn('Fill Report', mock_send.call_args[0][0])

    @patch('auto_feishu.send_feishu_message')
    def test_skipped_when_disabled(self, mock_send):
        cfg = {'dual_strategy': {'fill_feishu_enabled': False}}
        row = {'instrument_code': 'X', 'fill_side': 'buy_open', 'strategy': 'spread',
               'fill_price': '1', 'fill_volume': 1, 'bid_price': '', 'ask_price': '',
               'slippage_vs_mid': ''}
        self.assertFalse(notify_fill_trade(MagicMock(), {}, row, cfg))
        mock_send.assert_not_called()


class TestInstallUnified(unittest.TestCase):

    def setUp(self):
        import feishu_alert_cooldown as cooldown_mod
        from trade_feishu_notify import _SAVED_LEGACY

        cooldown_mod._PATCHED = False
        _SAVED_LEGACY.clear()
        autotrade_stubs.ensure_auto_feishu_stub()

    def test_suppresses_legacy_notify(self):
        import auto_feishu
        install_unified_trade_feishu()
        self.assertFalse(
            auto_feishu.notify_order_filled('IO', 'C', '0', 1, 1.0, config={})
        )

    def test_suppresses_combo_done_when_unified(self):
        import auto_feishu
        cfg = {
            'dual_strategy': {
                'unified_fill_feishu': True,
                'fill_feishu_enabled': True,
                'suppress_legacy_fill_feishu': True,
            },
        }
        install_unified_trade_feishu(cfg)
        self.assertFalse(
            auto_feishu.notify_combo_done(
                'IO', 'IO2604-C-4200', 2, 80.0, config=cfg,
            ),
        )
        self.assertFalse(
            auto_feishu.FeishuNotifier().notify_combo_done(
                'IO', 'IO2604-C-4200', 2, 80.0,
            ),
        )

    def test_suppress_legacy_flag(self):
        self.assertTrue(
            suppress_legacy_fill_feishu({
                'dual_strategy': {
                    'unified_fill_feishu': True,
                    'fill_feishu_enabled': True,
                    'suppress_legacy_fill_feishu': True,
                },
            }),
        )
        self.assertFalse(
            suppress_legacy_fill_feishu({
                'dual_strategy': {'unified_fill_feishu': False},
            }),
        )
        self.assertFalse(
            suppress_legacy_fill_feishu({
                'dual_strategy': {
                    'unified_fill_feishu': True,
                    'fill_feishu_enabled': False,
                },
            }),
        )


if __name__ == '__main__':
    unittest.main()
