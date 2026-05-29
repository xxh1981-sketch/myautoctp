"""spread_fill_sync unit tests"""

import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ctp_bootstrap  # noqa: F401

from pairtrade.constants import DIRECTION_BUY, DIRECTION_SELL, OFFSET_OPEN
from spread_fill_sync import (
    apply_spread_trade_record,
    count_spread_filled_open_orders,
    sync_csv_from_spread_trades,
)
from spread_ledger import SpreadLegStore
from pairtrade.constants import OFFSET_CLOSE
from import_spread_positions import apply_fill_to_spread_csv


class TestApplyFillToSpreadCsvReadFailure(unittest.TestCase):

    def test_read_failure_raises_and_preserves_file(self):
        """读已有 spread CSV 失败时必须抛出、保留原文件，绝不能空表续写抹认领。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'spread.csv')
            cfg = {'dual_strategy': {'spread_positions_csv': path}}
            with open(path, 'w', encoding='utf-8') as f:
                f.write('SA609C1000,2\nSA609C1100,not_an_int\n')
            before = open(path, encoding='utf-8').read()
            with self.assertRaises(Exception):
                apply_fill_to_spread_csv(
                    cfg, 'SA609C9999', DIRECTION_BUY, OFFSET_OPEN, 1, None,
                )
            self.assertEqual(open(path, encoding='utf-8').read(), before)


def _cfg(tmp, journal_name='spread_journal.jsonl'):
    csv_path = os.path.join(tmp, 'spread.csv')
    journal = os.path.join(tmp, journal_name)
    return {
        'strangle': {'order_ref_min': 500000},
        'dual_strategy': {
            'spread_order_ref_max': 499999,
            'spread_positions_csv': csv_path,
            'spread_trade_journal': journal,
            'journal_daily_shards': False,
        },
    }


class TestSpreadFillSync(unittest.TestCase):

    def test_strangle_trade_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            store = SpreadLegStore()
            ok = apply_spread_trade_record(cfg, store, {
                'order_ref': 500001,
                'instrument': 'MA609C2900',
                'direction': '0',
                'offset': '0',
                'volume': 1,
                'trade_id': 'T1',
            })
            self.assertFalse(ok)

    def test_spread_rejects_instrument_outside_tradeinfo(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            cfg['spread_tradeinfo'] = [{'future': 'MA', 'month': '609'}]
            store = SpreadLegStore()
            trade = {
                'order_ref': 50,
                'instrument': 'RM609C2650',
                'direction': DIRECTION_BUY,
                'offset': OFFSET_OPEN,
                'volume': 1,
                'trade_id': 'T_rm',
            }
            self.assertFalse(apply_spread_trade_record(cfg, store, trade))
            self.assertEqual(store.list_leg_claims(), {})
            journal = cfg['dual_strategy']['spread_trade_journal']
            with open(journal, 'r', encoding='utf-8') as f:
                body = f.read()
            self.assertIn('skipped', body)
            self.assertIn('not_in_spread_tradeinfo', body)

    def test_spread_buy_open_updates_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            store = SpreadLegStore()
            trade = {
                'order_ref': 100,
                'instrument': 'MA609C2900',
                'direction': DIRECTION_BUY,
                'offset': OFFSET_OPEN,
                'volume': 1,
                'trade_id': 'T2',
            }
            self.assertTrue(apply_spread_trade_record(cfg, store, trade))
            self.assertFalse(apply_spread_trade_record(cfg, store, trade))
            self.assertEqual(store.list_leg_claims()['MA609C2900'], 1)

    def test_skip_spread_fill_when_only_strangle_claims(self):
        """宽跨-only 合约 + 价差段 ref：勿污染 spread_positions.csv。"""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            cfg['spread_tradeinfo'] = [{'future': 'rb', 'month': '2610'}]
            store = SpreadLegStore()
            ledger = MagicMock()
            ledger.list_leg_claims.return_value = {'rb2610C3450': 1}
            ledger.list_unmatched_legs.return_value = []
            conn = MagicMock()
            conn._runtime_state = {'_strangle_ledger': ledger}
            cfg['_spread_fill_conn'] = conn
            trade = {
                'order_ref': 892,
                'instrument': 'rb2610C3450',
                'direction': DIRECTION_SELL,
                'offset': OFFSET_CLOSE,
                'volume': 1,
                'trade_id': 'RB_CLOSE',
            }
            self.assertFalse(apply_spread_trade_record(cfg, store, trade))
            self.assertEqual(store.list_leg_claims(), {})
            journal = cfg['dual_strategy']['spread_trade_journal']
            with open(journal, 'r', encoding='utf-8') as f:
                body = f.read()
            self.assertIn('strangle_owned_only', body)

    def test_allow_spread_fill_when_spread_store_has_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            store = SpreadLegStore()
            store.set_leg_claims({'MA609C2900': 1})
            ledger = MagicMock()
            ledger.list_leg_claims.return_value = {'MA609C2900': 2}
            ledger.list_unmatched_legs.return_value = []
            conn = MagicMock()
            conn._runtime_state = {'_strangle_ledger': ledger}
            cfg['_spread_fill_conn'] = conn
            trade = {
                'order_ref': 102,
                'instrument': 'MA609C2900',
                'direction': DIRECTION_SELL,
                'offset': OFFSET_CLOSE,
                'volume': 1,
                'trade_id': 'T_close',
            }
            self.assertTrue(apply_spread_trade_record(cfg, store, trade))

    def test_spread_sell_open_short(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            store = SpreadLegStore()
            trade = {
                'order_ref': 101,
                'instrument': 'MA609C2950',
                'direction': DIRECTION_SELL,
                'offset': OFFSET_OPEN,
                'volume': 2,
                'trade_id': 'T3',
            }
            self.assertTrue(apply_spread_trade_record(cfg, store, trade))
            self.assertEqual(store.list_leg_claims()['MA609C2950'], -2)

    def test_sync_from_query_replays_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            store = SpreadLegStore()
            conn = MagicMock()
            conn.query_trades_sync.return_value = [
                {
                    'order_ref': 200,
                    'instrument': 'MA609C2900',
                    'direction': '0',
                    'offset': '0',
                    'volume': 1,
                    'price': 100.0,
                    'trade_id': 'Q1',
                    'trade_date': '20260520',
                    'trade_time': '10:00:00',
                },
                {
                    'order_ref': 500010,
                    'instrument': 'SA609P900',
                    'direction': '0',
                    'offset': '0',
                    'volume': 99,
                    'trade_id': 'Q2',
                },
            ]
            n = sync_csv_from_spread_trades(conn, store, cfg, logger=None)
            self.assertEqual(n, 1)
            self.assertEqual(store.long_call_volumes().get('MA609C2900'), 1)

    def test_count_spread_filled_open_orders(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            conn = MagicMock()
            conn.query_orders_sync.return_value = [
                {'order_ref': 1, 'offset': '0', 'status': '0', 'insert_date': '20260520'},
                {'order_ref': 500001, 'offset': '0', 'status': '0', 'insert_date': '20260520'},
                {'order_ref': 2, 'offset': '1', 'status': '0', 'insert_date': '20260520'},
            ]
            from datetime import datetime
            with unittest.mock.patch('spread_fill_sync.datetime') as mock_dt:
                mock_dt.now.return_value = datetime(2026, 5, 20)
                count = count_spread_filled_open_orders(conn, cfg, timeout=1)
            self.assertEqual(count, 1)

    def test_count_excludes_partial_fills_and_other_days(self):
        """Match autotrade get_filled_open_order_count: only status='0' (full
        fill) on today's spread-segment open orders is counted."""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            conn = MagicMock()
            conn.query_orders_sync.return_value = [
                {'order_ref': 10, 'offset': '0', 'status': '1', 'insert_date': '20260520'},
                {'order_ref': 11, 'offset': '0', 'status': '5', 'insert_date': '20260520'},
                {'order_ref': 12, 'offset': '0', 'status': '0', 'insert_date': '20260519'},
                {'order_ref': 13, 'offset': '0', 'status': '0', 'insert_date': '20260520'},
            ]
            from datetime import datetime
            with unittest.mock.patch('spread_fill_sync.datetime') as mock_dt:
                mock_dt.now.return_value = datetime(2026, 5, 20)
                count = count_spread_filled_open_orders(conn, cfg, timeout=1)
            self.assertEqual(count, 1)

    def test_sync_from_query_reuses_injected_trades(self):
        """When caller provides trades, sync must NOT issue another CTP query."""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            store = SpreadLegStore()
            conn = MagicMock()
            conn.query_trades_sync.side_effect = AssertionError(
                'query_trades_sync should be skipped when trades= provided'
            )
            trades = [
                {
                    'order_ref': 201,
                    'instrument': 'MA609C2950',
                    'direction': '1',
                    'offset': '0',
                    'volume': 2,
                    'price': 80.0,
                    'trade_id': 'INJ',
                    'trade_date': '20260520',
                    'trade_time': '10:00:00',
                },
            ]
            n = sync_csv_from_spread_trades(conn, store, cfg, logger=None, trades=trades)
            self.assertEqual(n, 1)
            self.assertEqual(store.list_leg_claims()['MA609C2950'], -2)


if __name__ == '__main__':
    unittest.main()
