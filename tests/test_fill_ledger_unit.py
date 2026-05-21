"""fill_ledger 纯逻辑 unit tests（不依赖 ctp_bootstrap）。"""

import builtins
import csv
import os
import sys
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import autotrade_stubs

autotrade_stubs.ensure_autotrade_stubs([
    'auto_strategy_order_ref',
    'auto_connection_utils',
])

from fill_ledger import (
    FILL_LEDGER_COLUMNS,
    append_fill_row,
    apply_fill_record,
    build_fill_row,
    fill_ledger_csv_path,
    fill_ledger_journal_path,
    resolve_fill_side,
    resolve_strategy,
    slippage_vs_mid,
    sync_fill_ledger_from_trades,
    _ensure_csv_header,
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
            'journal_daily_shards': False,
        },
    }


class TestFillLedgerPaths(unittest.TestCase):

    def test_relative_paths_under_project(self):
        cfg = {'dual_strategy': {
            'fill_ledger_csv': 'data/custom.csv',
            'fill_ledger_journal': 'data/custom.jsonl',
        }}
        self.assertTrue(fill_ledger_csv_path(cfg).replace('\\', '/').endswith('/data/custom.csv'))
        self.assertTrue(fill_ledger_journal_path(cfg).replace('\\', '/').endswith('/data/custom.jsonl'))


class TestFillSide(unittest.TestCase):

    def test_buy_open_and_sell_close(self):
        self.assertEqual(resolve_fill_side('0', '0'), 'buy_open')
        self.assertEqual(resolve_fill_side('1', '1'), 'sell_close')

    def test_buy_close_multi_char_offset(self):
        self.assertEqual(resolve_fill_side('0', '31'), 'buy_close')


class TestSlippage(unittest.TestCase):

    def test_buy_and_sell_adverse(self):
        self.assertEqual(slippage_vs_mid(100.5, 100.0, 100.2, 'buy_open'), '0.4000')
        self.assertEqual(slippage_vs_mid(99.8, 100.0, 100.2, 'sell_open'), '0.3000')
        self.assertEqual(slippage_vs_mid(100.0, 0, 0, 'buy_open'), '')


class TestStrategy(unittest.TestCase):

    def test_spread_strangle_other(self):
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
            with open(cfg['dual_strategy']['fill_ledger_csv'], encoding='utf-8') as f:
                rows = list(csv.reader(f))
            self.assertEqual(rows[0], FILL_LEDGER_COLUMNS)
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
        self.assertEqual(row['slippage_vs_mid'], '0.1000')
        self.assertEqual(row['strategy'], 'spread')

    def test_sync_from_query(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            conn = MagicMock()
            conn.quotes = {}
            conn.option_quotes = {}
            conn.query_trades_sync.return_value = [{
                'order_ref': 50,
                'instrument': 'IO2604-C-4000',
                'direction': '0',
                'offset': '0',
                'volume': 1,
                'price': 12.5,
                'trade_id': 'Q1',
            }]
            self.assertEqual(sync_fill_ledger_from_trades(conn, cfg), 1)


class TestAppendRowAtomicity(unittest.TestCase):

    def test_multi_appends_have_consistent_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            conn = MagicMock()
            conn.quotes = {}
            conn.option_quotes = {}
            for i in range(3):
                apply_fill_record(conn, cfg, {
                    'order_ref': 500000 + i,
                    'instrument': f'SA609C{1000 + i}',
                    'direction': '0',
                    'offset': '0',
                    'volume': 1,
                    'price': 50.0,
                    'trade_id': f'T{i}',
                })
            with open(cfg['dual_strategy']['fill_ledger_csv'], encoding='utf-8') as f:
                rows = list(csv.reader(f))
            self.assertEqual(len(rows), 4)
            for row in rows:
                self.assertEqual(len(row), len(FILL_LEDGER_COLUMNS))

    def test_append_uses_single_file_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'f.csv')
            _ensure_csv_header(path)
            write_calls = []
            real_open = builtins.open

            class _SpyFile:
                def __init__(self, f):
                    self._f = f

                def write(self, s):
                    write_calls.append(s)
                    return self._f.write(s)

                def flush(self):
                    return self._f.flush()

                def fileno(self):
                    return self._f.fileno()

                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    self._f.close()
                    return False

            def _spy_open(p, mode='r', *a, **kw):
                f = real_open(p, mode, *a, **kw)
                if p == path and 'a' in mode:
                    return _SpyFile(f)
                return f

            row = {col: 'v' for col in FILL_LEDGER_COLUMNS}
            with patch('builtins.open', _spy_open):
                append_fill_row(path, row)
            self.assertEqual(len(write_calls), 1)


class TestApplyFillRecordIdempotency(unittest.TestCase):

    def test_concurrent_applies_record_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            conn = MagicMock()
            conn.quotes = {}
            conn.option_quotes = {}
            trade = {
                'order_ref': 500099,
                'instrument': 'SA609C1000',
                'direction': '0',
                'offset': '0',
                'volume': 1,
                'price': 50.0,
                'trade_id': 'DUP',
            }
            results = []

            def _worker():
                results.append(apply_fill_record(conn, cfg, trade))

            threads = [threading.Thread(target=_worker) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(sum(1 for r in results if r), 1)


if __name__ == '__main__':
    unittest.main()
