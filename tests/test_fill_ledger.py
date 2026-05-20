"""fill_ledger unit tests"""

import csv
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ctp_bootstrap  # noqa: F401

from fill_ledger import (
    FILL_LEDGER_COLUMNS,
    apply_fill_record,
    build_fill_row,
    resolve_fill_side,
    resolve_strategy,
    slippage_vs_mid,
    sync_fill_ledger_from_trades,
    trade_dedupe_key,
    wire_fill_ledger,
)


def _cfg(tmp):
    csv_path = os.path.join(tmp, 'fills.csv')
    journal = os.path.join(tmp, 'journal.jsonl')
    return {
        'strangle': {'order_ref_min': 500000},
        'dual_strategy': {
            'spread_order_ref_max': 499999,
            'fill_ledger_csv': csv_path,
            'fill_ledger_journal': journal,
        },
    }


class TestFillSide(unittest.TestCase):

    def test_buy_open(self):
        self.assertEqual(resolve_fill_side('0', '0'), 'buy_open')

    def test_sell_close(self):
        self.assertEqual(resolve_fill_side('1', '1'), 'sell_close')

    def test_buy_close_today(self):
        self.assertEqual(resolve_fill_side('0', '3'), 'buy_close')


class TestSlippage(unittest.TestCase):

    def test_buy_adverse(self):
        self.assertEqual(slippage_vs_mid(100.5, 100.0, 100.2, 'buy_open'), '0.4000')

    def test_sell_adverse(self):
        self.assertEqual(slippage_vs_mid(99.8, 100.0, 100.2, 'sell_open'), '0.3000')

    def test_missing_quote(self):
        self.assertEqual(slippage_vs_mid(100.0, 0, 0, 'buy_open'), '')


class TestStrategy(unittest.TestCase):

    def test_spread_and_strangle(self):
        cfg = {'strangle': {'order_ref_min': 500000}, 'dual_strategy': {'spread_order_ref_max': 499999}}
        self.assertEqual(resolve_strategy(100, cfg), 'spread')
        self.assertEqual(resolve_strategy(500000, cfg), 'strangle')
        self.assertEqual(resolve_strategy(0, cfg), 'other')


class TestFillLedgerCsv(unittest.TestCase):

    def test_append_and_dedupe(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            conn = MagicMock()
            conn.quotes = {}
            conn.option_quotes = {}
            trade = {
                'order_ref': 500001,
                'instrument': 'SA609C1000',
                'direction': '0',
                'offset': '0',
                'volume': 2,
                'price': 50.25,
                'trade_id': 'T1',
            }
            self.assertTrue(apply_fill_record(conn, cfg, trade))
            self.assertFalse(apply_fill_record(conn, cfg, trade))

            csv_path = cfg['dual_strategy']['fill_ledger_csv']
            with open(csv_path, encoding='utf-8') as f:
                rows = list(csv.reader(f))
            self.assertEqual(rows[0], FILL_LEDGER_COLUMNS)
            self.assertEqual(rows[1][0], 'SA609C1000')
            self.assertEqual(rows[1][6], 'buy_open')
            self.assertEqual(rows[1][7], 'strangle')

    def test_build_row_with_quote(self):
        conn = MagicMock()
        q = MagicMock()
        q.bid = 100.0
        q.ask = 100.4
        conn.quotes = {'SA609C1000': q}
        conn.option_quotes = {}
        cfg = {'strangle': {'order_ref_min': 500000}, 'dual_strategy': {'spread_order_ref_max': 499999}}
        row = build_fill_row(conn, {
            'order_ref': 10,
            'instrument': 'SA609C1000',
            'direction': '0',
            'offset': '0',
            'volume': 1,
            'price': 100.3,
        }, cfg)
        self.assertEqual(row['bid_price'], '100.0000')
        self.assertEqual(row['ask_price'], '100.4000')
        self.assertEqual(row['slippage_vs_mid'], '0.1000')
        self.assertEqual(row['strategy'], 'spread')

    def test_sync_from_query(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            conn = MagicMock()
            conn.quotes = {}
            conn.option_quotes = {}
            conn.query_trades_sync.return_value = [
                {
                    'order_ref': 50,
                    'instrument': 'IO2604-C-4000',
                    'direction': '0',
                    'offset': '0',
                    'volume': 1,
                    'price': 12.5,
                    'trade_id': 'Q1',
                    'trade_date': '20260520',
                    'trade_time': '10:00:00',
                },
            ]
            n = sync_fill_ledger_from_trades(conn, cfg, logger=None)
            self.assertEqual(n, 1)
            journal = cfg['dual_strategy']['fill_ledger_journal']
            self.assertEqual(len(open(journal, encoding='utf-8').read().strip().splitlines()), 1)


class TestWireFillLedger(unittest.TestCase):

    def test_chains_handler(self):
        conn = MagicMock()
        conn._runtime_state = {}
        called = []

        def prev(c, p, l):
            called.append('prev')

        conn._runtime_state['_strangle_trade_handler'] = prev
        wire_fill_ledger(conn)

        p_trade = MagicMock()
        p_trade.OrderRef = '500001'
        p_trade.InstrumentID = b'SA609C1000'
        p_trade.Direction = b'0'
        p_trade.OffsetFlag = b'0'
        p_trade.Volume = 1
        p_trade.Price = 50.0
        p_trade.TradeID = b'T99'
        p_trade.TradeDate = b'20260520'
        p_trade.TradeTime = b'10:00:00'
        conn.config = _cfg(tempfile.mkdtemp())

        handler = conn._runtime_state['_strangle_trade_handler']
        handler(conn, p_trade, None)
        self.assertEqual(called, ['prev'])


if __name__ == '__main__':
    unittest.main()
