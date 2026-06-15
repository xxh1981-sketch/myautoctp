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
    'combo_id_registry',
])

from fill_ledger import (
    FILL_LEDGER_COLUMNS,
    _warn_bilateral_orderref_suspicious,
    append_fill_row,
    apply_fill_record,
    build_fill_row,
    detect_bilateral_orderref_suspicious,
    fill_ledger_csv_path,
    fill_ledger_journal_path,
    pop_fill_csv_status,
    resolve_fill_side,
    resolve_strategy,
    slippage_vs_mid,
    sync_fill_ledger_from_trades,
    _ensure_csv_header,
)
from spread_ledger import SpreadLegStore
from trade_journal import trade_dedupe_key


def _cfg(tmp):
    csv_path = os.path.join(tmp, 'fills.csv')
    journal = os.path.join(tmp, 'journal.jsonl')
    spread_journal = os.path.join(tmp, 'spread_journal.jsonl')
    return {
        'strangle': {'order_ref_min': 500000},
        'dual_strategy': {
            'spread_order_ref_max': 499999,
            'fill_ledger_csv': csv_path,
            'fill_ledger_journal': journal,
            'spread_trade_journal': spread_journal,
            'journal_daily_shards': False,
            'trade_replay_lookback_days': 0,
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

    def test_legacy_direction_aliases_still_buy(self):
        self.assertEqual(resolve_fill_side('Buy', '0'), 'buy_open')

    def test_unknown_direction_warns_but_keeps_legacy_mapping(self):
        logger = MagicMock()
        self.assertEqual(
            resolve_fill_side('bad', '0', logger=logger, context='ctx'),
            'sell_open',
        )
        logger.warning.assert_called_once()
        self.assertIn('未知 Direction', logger.warning.call_args.args[0])

    def test_unknown_field_warning_can_be_disabled(self):
        logger = MagicMock()
        self.assertEqual(resolve_fill_side('bad', 'bad', logger=logger, warn_unknown=False), 'sell_close')
        logger.warning.assert_not_called()


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


class TestBilateralOrderRefDetector(unittest.TestCase):

    def test_same_order_ref_and_instrument_buy_then_sell_is_suspicious(self):
        runtime = {}
        cfg = {'dual_strategy': {'orderref_bilateral_nonzero_alert': True}}
        suspicious = detect_bilateral_orderref_suspicious([
            {'order_ref': 500001, 'instrument': 'SA609C1000', 'direction': '0', 'offset': '0', 'volume': 1, 'trade_id': 'B1'},
            {'order_ref': 500001, 'instrument': 'SA609C1000', 'direction': '1', 'offset': '0', 'volume': 1, 'trade_id': 'S1'},
        ], config=cfg, runtime=runtime)
        self.assertEqual(len(suspicious), 1)
        self.assertEqual(suspicious[0]['order_ref'], 500001)
        self.assertEqual(suspicious[0]['instrument'], 'SA609C1000')

    def test_different_instrument_is_not_suspicious(self):
        suspicious = detect_bilateral_orderref_suspicious([
            {'order_ref': 500001, 'instrument': 'SA609C1000', 'direction': '0', 'offset': '0', 'volume': 1},
            {'order_ref': 500001, 'instrument': 'SA609C1100', 'direction': '1', 'offset': '0', 'volume': 1},
        ], config={'dual_strategy': {'orderref_bilateral_nonzero_alert': True}})
        self.assertEqual(suspicious, [])

    def test_different_order_ref_is_not_suspicious(self):
        suspicious = detect_bilateral_orderref_suspicious([
            {'order_ref': 500001, 'instrument': 'SA609C1000', 'direction': '0', 'offset': '0', 'volume': 1},
            {'order_ref': 500002, 'instrument': 'SA609C1000', 'direction': '1', 'offset': '0', 'volume': 1},
        ], config={'dual_strategy': {'orderref_bilateral_nonzero_alert': True}})
        self.assertEqual(suspicious, [])

    def test_zero_volume_is_ignored(self):
        suspicious = detect_bilateral_orderref_suspicious([
            {'order_ref': 500001, 'instrument': 'SA609C1000', 'direction': '0', 'offset': '0', 'volume': 0},
            {'order_ref': 500001, 'instrument': 'SA609C1000', 'direction': '1', 'offset': '0', 'volume': 1},
        ], config={'dual_strategy': {'orderref_bilateral_nonzero_alert': True}})
        self.assertEqual(suspicious, [])

    def test_detector_can_be_disabled(self):
        suspicious = detect_bilateral_orderref_suspicious([
            {'order_ref': 500001, 'instrument': 'SA609C1000', 'direction': '0', 'offset': '0', 'volume': 1},
            {'order_ref': 500001, 'instrument': 'SA609C1000', 'direction': '1', 'offset': '0', 'volume': 1},
        ], config={'dual_strategy': {'orderref_bilateral_nonzero_alert': False}})
        self.assertEqual(suspicious, [])

    def test_cooldown_blocks_repeated_alerts(self):
        runtime = {'_margin_halt_open': False}
        cfg = {'dual_strategy': {'orderref_bilateral_nonzero_alert_cooldown_sec': 60}}
        logger = MagicMock()
        trades = [
            {'order_ref': 500001, 'instrument': 'SA609C1000', 'direction': '0', 'offset': '0', 'volume': 1, 'trade_id': 'B1'},
            {'order_ref': 500001, 'instrument': 'SA609C1000', 'direction': '1', 'offset': '0', 'volume': 1, 'trade_id': 'S1'},
        ]
        first = detect_bilateral_orderref_suspicious(trades, cfg, logger, runtime)
        self.assertEqual(len(first), 1)
        _warn_bilateral_orderref_suspicious(first, cfg, logger, runtime)
        logger.warning.assert_called_once()

        second = detect_bilateral_orderref_suspicious(trades, cfg, logger, runtime)
        self.assertEqual(len(second), 1)
        _warn_bilateral_orderref_suspicious(second, cfg, logger, runtime)
        logger.warning.assert_called_once()

    def test_seen_persists_across_calls_on_runtime(self):
        runtime = {'_margin_halt_open': False}
        cfg = {'dual_strategy': {'orderref_bilateral_nonzero_alert': True}}
        buy = {
            'order_ref': 500001, 'instrument': 'SA609C1000',
            'fill_side': 'buy_open', 'volume': 1, 'trade_id': 'B1',
        }
        sell = {
            'order_ref': 500001, 'instrument': 'SA609C1000',
            'fill_side': 'sell_open', 'volume': 1, 'trade_id': 'S1',
        }
        self.assertEqual(detect_bilateral_orderref_suspicious([buy], cfg, runtime=runtime), [])
        suspicious = detect_bilateral_orderref_suspicious([sell], cfg, runtime=runtime)
        self.assertEqual(len(suspicious), 1)

    def test_warn_logs_with_nonempty_runtime(self):
        runtime = {'_margin_halt_open': False}
        cfg = {'dual_strategy': {'orderref_bilateral_nonzero_alert': True}}
        logger = MagicMock()
        trades = [
            {'order_ref': 500001, 'instrument': 'SA609C1000', 'fill_side': 'buy_open', 'volume': 1, 'trade_id': 'B1'},
            {'order_ref': 500001, 'instrument': 'SA609C1000', 'fill_side': 'sell_open', 'volume': 1, 'trade_id': 'S1'},
        ]
        suspicious = detect_bilateral_orderref_suspicious(trades, cfg, logger, runtime)
        _warn_bilateral_orderref_suspicious(suspicious, cfg, logger, runtime)
        logger.warning.assert_called_once()


class TestFillLedgerCsv(unittest.TestCase):

    def test_append_and_dedupe(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            conn = MagicMock()
            conn._runtime_state = {}
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
            self.assertEqual(rows[1][8], 'unknown')
            self.assertEqual(rows[1][9], '')

    def test_build_row_with_quote(self):
        conn = MagicMock()
        q = MagicMock()
        q.bid = 100.0
        q.ask = 100.4
        conn.quotes = {'SA609C1000': q}
        conn.option_quotes = {}
        conn._combo_id_by_order_ref = {10: 'spread-SA-open-123'}
        cfg = {'strangle': {'order_ref_min': 500000}, 'dual_strategy': {'spread_order_ref_max': 499999}}
        row = build_fill_row(conn, {
            'order_ref': 10,
            'instrument': 'SA609C1000',
            'direction': '0',
            'offset': '0',
            'volume': 1,
            'price': 100.3,
            'trade_date': '20260609',
            'trade_time': '09:30:01',
        }, cfg)
        self.assertEqual(row['slippage_vs_mid'], '0.1000')
        self.assertEqual(row['strategy'], 'spread')
        self.assertEqual(row['trade_date'], '20260609')
        self.assertEqual(row['trade_time'], '09:30:01')
        self.assertEqual(row['combo_id'], 'spread-SA-open-123')

    def test_build_row_warns_on_unknown_direction(self):
        conn = MagicMock()
        conn.quotes = {}
        conn.option_quotes = {}
        logger = MagicMock()
        cfg = {'strangle': {'order_ref_min': 500000}, 'dual_strategy': {'spread_order_ref_max': 499999}}
        row = build_fill_row(conn, {
            'order_ref': 10,
            'instrument': 'SA609C1000',
            'direction': 'bad',
            'offset': '0',
            'volume': 1,
            'price': 100.3,
        }, cfg, logger=logger)
        self.assertEqual(row['fill_side'], 'sell_open')
        logger.warning.assert_called_once()

    def test_build_row_skips_bad_volume_and_price(self):
        conn = MagicMock()
        conn.quotes = {}
        conn.option_quotes = {}
        logger = MagicMock()
        cfg = {'strangle': {'order_ref_min': 500000}, 'dual_strategy': {}}
        self.assertIsNone(build_fill_row(conn, {
            'order_ref': 10,
            'instrument': 'SA609C1000',
            'direction': '0',
            'offset': '0',
            'volume': 'abc',
            'price': 100.3,
        }, cfg, logger=logger))
        self.assertIsNone(build_fill_row(conn, {
            'order_ref': 10,
            'instrument': 'SA609C1000',
            'direction': '0',
            'offset': '0',
            'volume': 1,
            'price': 'bad',
        }, cfg, logger=logger))
        self.assertEqual(logger.warning.call_count, 2)

    def test_detect_bilateral_skips_non_dict_trades(self):
        cfg = {'dual_strategy': {'orderref_bilateral_nonzero_alert': True}}
        suspicious = detect_bilateral_orderref_suspicious(
            [None, 'bad', {
                'order_ref': 1,
                'instrument': 'SA609C1000',
                'volume': 1,
                'direction': '0',
                'offset': '0',
            }],
            cfg,
        )
        self.assertEqual(suspicious, [])

    def test_sync_from_query(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            conn = MagicMock()
            conn._runtime_state = {}
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

    def test_sync_skips_bilateral_alert_for_applied_trades(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            conn = MagicMock()
            conn._runtime_state = {}
            conn.quotes = {}
            conn.option_quotes = {}
            logger = MagicMock()
            trades = [
                {
                    'order_ref': 50,
                    'instrument': 'IO2604-C-4000',
                    'direction': '0',
                    'offset': '0',
                    'volume': 1,
                    'price': 12.5,
                    'trade_id': 'Q1',
                },
                {
                    'order_ref': 50,
                    'instrument': 'IO2604-C-4000',
                    'direction': '1',
                    'offset': '1',
                    'volume': 1,
                    'price': 12.6,
                    'trade_id': 'Q2',
                },
            ]
            applied_keys = {
                trade_dedupe_key(trades[0]),
                trade_dedupe_key(trades[1]),
            }
            with patch('fill_ledger.load_applied_keys', return_value=applied_keys), \
                 patch('fill_ledger.apply_fill_record') as mock_apply:
                self.assertEqual(
                    sync_fill_ledger_from_trades(conn, cfg, logger=logger, trades=trades),
                    0,
                )
            logger.warning.assert_not_called()
            mock_apply.assert_not_called()


class TestAppendRowAtomicity(unittest.TestCase):

    def test_multi_appends_have_consistent_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            conn = MagicMock()
            conn._runtime_state = {}
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
            conn._runtime_state = {}
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


class TestFillLedgerCsvStatus(unittest.TestCase):

    def test_spread_skip_reflected_in_fill_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            cfg['spread_tradeinfo'] = [{'future': 'rb', 'month': '2610'}]
            store = SpreadLegStore()
            ledger = MagicMock()
            ledger.list_leg_claims.return_value = {'rb2610C3450': 1}
            ledger.list_unmatched_legs.return_value = []
            conn = MagicMock()
            conn._runtime_state = {'_strangle_ledger': ledger}
            conn.quotes = {}
            conn.option_quotes = {}
            cfg['_spread_fill_conn'] = conn
            trade = {
                'order_ref': 892,
                'instrument': 'rb2610C3450',
                'direction': '1',
                'offset': '1',
                'volume': 1,
                'price': 50.0,
                'trade_id': 'RB_CLOSE',
            }
            from spread_fill_sync import apply_spread_trade_record
            self.assertFalse(apply_spread_trade_record(cfg, store, trade))
            self.assertTrue(apply_fill_record(conn, cfg, trade))
            with open(cfg['dual_strategy']['fill_ledger_csv'], encoding='utf-8') as f:
                rows = list(csv.reader(f))
            self.assertEqual(rows[1][7], 'spread')
            self.assertEqual(rows[1][8], 'no')
            self.assertEqual(rows[1][9], 'strangle_owned_only')


if __name__ == '__main__':
    unittest.main()
