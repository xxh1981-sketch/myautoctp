"""P4 banner overlap + 异常 OrderRef 告警测试"""

import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ctp_bootstrap  # noqa: F401


class TestBannerOverlap(unittest.TestCase):

    def _cap_logger(self):
        cap = MagicMock()
        cap.messages = []

        def _info(msg, *a, **kw):
            cap.messages.append(('INFO', msg))

        def _warning(msg, *a, **kw):
            cap.messages.append(('WARNING', msg))

        cap.info = _info
        cap.warning = _warning
        return cap

    def test_overlap_logged_as_warning(self):
        from merged_main import _log_banner

        logger = self._cap_logger()
        config = {
            'VIX_TRIGGER_MULTIPLIER': 1.0,
            'daily_trade_limit': 100,
            'strangle': {'benchmark_multiplier': 0.8, 'daily_buy_limit_yuan': 300000},
            'loop_interval': 10,
        }
        spread_info = [
            {'future': 'SA', 'month': '609'},
            {'future': 'RM', 'month': '509'},
        ]
        strangle_info = [
            {'future': 'SA', 'month': '609'},
            {'future': 'MA', 'month': '609'},
        ]
        _log_banner(config, spread_info, strangle_info, logger)
        warnings = [m for lvl, m in logger.messages if lvl == 'WARNING']
        self.assertTrue(
            any('同覆盖品种' in m and 'SA' in m for m in warnings),
            f'expected overlap warning for SA, got {warnings}',
        )

    def test_no_overlap_no_warning(self):
        from merged_main import _log_banner

        logger = self._cap_logger()
        config = {
            'strangle': {'benchmark_multiplier': 0.8, 'daily_buy_limit_yuan': 300000},
        }
        _log_banner(
            config,
            [{'future': 'SA', 'month': '609'}],
            [{'future': 'MA', 'month': '609'}],
            logger,
        )
        warnings = [m for lvl, m in logger.messages if lvl == 'WARNING']
        self.assertFalse(
            any('同覆盖品种' in m for m in warnings),
            'unexpected overlap warning',
        )


class TestUnexpectedSpreadSymbolWarning(unittest.TestCase):

    def setUp(self):
        # 清理跨用例残留
        import spread_fill_sync as sfs
        sfs._UNEXPECTED_SPREAD_SYMBOL_WARNED.clear()
        self.tmp = tempfile.mkdtemp()

    def _cfg(self):
        return {
            'spread_tradeinfo': [{'future': 'SA', 'month': '609'}],
            'strangle_tradeinfo': [
                {'future': 'SA', 'month': '609'},
                {'future': 'RM', 'month': '509'},
            ],
            'dual_strategy': {
                'spread_trade_journal': os.path.join(self.tmp, 'spread_journal.jsonl'),
                'spread_positions_csv_path': os.path.join(self.tmp, 'spread.csv'),
                # P4 用例：OrderRef 在价差段但品种不在 spread_tradeinfo → 仍入账，每品种 warning 一次
                'spread_fill_require_tradeinfo_match': False,
            },
            'strategy_order_ref': {
                'spread_min': 1, 'spread_max': 499999,
                'strangle_min': 500000, 'strangle_max': 999999,
            },
        }

    def test_warns_when_spread_orderref_hits_strangle_symbol(self):
        from spread_fill_sync import apply_spread_trade_record

        logger = MagicMock()
        cfg = self._cfg()
        store = MagicMock()

        # OrderRef=100 命中 spread 段；instrument 是 RM（仅在 strangle 配置中）
        trade = {
            'order_ref': 100,
            'instrument': 'RM509-C-9000',
            'direction': '0',
            'offset': '0',
            'volume': 1,
            'price': 100.0,
            'trade_id': 'T1',
            'trade_date': '20260520',
            'trade_time': '10:00:00',
        }
        applied = apply_spread_trade_record(cfg, store, trade, logger)
        self.assertTrue(applied)
        warning_msgs = [c.args[0] for c in logger.warning.call_args_list]
        self.assertTrue(
            any('RM' in m and '价差段' in m for m in warning_msgs),
            f'expected RM warning, got {warning_msgs}',
        )

    def test_does_not_warn_for_configured_spread_symbol(self):
        from spread_fill_sync import apply_spread_trade_record

        logger = MagicMock()
        cfg = self._cfg()
        store = MagicMock()
        trade = {
            'order_ref': 200,
            'instrument': 'SA609C1000',
            'direction': '0',
            'offset': '0',
            'volume': 1,
            'price': 50.0,
            'trade_id': 'T2',
            'trade_date': '20260520',
            'trade_time': '10:00:00',
        }
        apply_spread_trade_record(cfg, store, trade, logger)
        warning_msgs = [c.args[0] for c in logger.warning.call_args_list]
        self.assertFalse(
            any('价差段' in m for m in warning_msgs),
            f'unexpected warning for configured SA: {warning_msgs}',
        )

    def test_warns_only_once_per_symbol(self):
        from spread_fill_sync import apply_spread_trade_record

        logger = MagicMock()
        cfg = self._cfg()
        store = MagicMock()
        for i in range(5):
            trade = {
                'order_ref': 300 + i,
                'instrument': 'RM509-C-9000',
                'direction': '0',
                'offset': '0',
                'volume': 1,
                'price': 100.0,
                'trade_id': f'T_warn_{i}',
                'trade_date': '20260520',
                'trade_time': '10:00:00',
            }
            apply_spread_trade_record(cfg, store, trade, logger)
        warning_msgs = [
            c.args[0] for c in logger.warning.call_args_list
            if '价差段' in c.args[0]
        ]
        self.assertEqual(
            len(warning_msgs), 1,
            f'expected exactly 1 warning per symbol, got {warning_msgs}',
        )


if __name__ == '__main__':
    unittest.main()
