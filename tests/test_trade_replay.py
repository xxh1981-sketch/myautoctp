"""trade_replay unit tests."""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trade_replay import (
    load_cached_trades,
    merge_trades_for_replay,
    purge_invalid_cache_entries,
    record_trades_to_cache,
    trade_replay_lookback_days,
)


def _cfg(tmp):
    return {
        'dual_strategy': {
            'trade_replay_cache': os.path.join(tmp, 'cache.jsonl'),
            'trade_replay_lookback_days': 1,
        },
    }


class TestTradeReplay(unittest.TestCase):

    def test_record_and_merge_dedupes(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            t1 = {
                'order_ref': 1,
                'instrument': 'SA609C2400',
                'direction': '0',
                'offset': '0',
                'volume': 1,
                'price': 100.0,
                'trade_id': '900001',
                'trade_date': '20260610',
            }
            self.assertEqual(record_trades_to_cache([t1], cfg), 1)
            self.assertEqual(record_trades_to_cache([t1], cfg), 0)
            merged = merge_trades_for_replay([], cfg)
            self.assertEqual(len(merged), 1)
            merged2 = merge_trades_for_replay([t1], cfg)
            self.assertEqual(len(merged2), 1)

    def test_live_plus_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            old = {
                'order_ref': 2,
                'instrument': 'SA609C2500',
                'direction': '0',
                'offset': '0',
                'volume': 2,
                'price': 50.0,
                'trade_id': '900002',
                'trade_date': '20260610',
            }
            new = {
                'order_ref': 3,
                'instrument': 'SA609C2600',
                'direction': '0',
                'offset': '0',
                'volume': 1,
                'price': 60.0,
                'trade_id': '900003',
                'trade_date': '20260611',
            }
            record_trades_to_cache([old], cfg)
            merged = merge_trades_for_replay([new], cfg)
            keys = {t['trade_id'] for t in merged}
            self.assertEqual(keys, {'900002', '900003'})

    def test_lookback_zero_disables_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            cfg['dual_strategy']['trade_replay_lookback_days'] = 0
            self.assertEqual(trade_replay_lookback_days(cfg), 0)
            t = {
                'order_ref': 1,
                'instrument': 'X',
                'direction': '0',
                'offset': '0',
                'volume': 1,
                'price': 1.0,
                'trade_id': '900004',
                'trade_date': '20260610',
            }
            self.assertEqual(record_trades_to_cache([t], cfg), 0)
            self.assertEqual(load_cached_trades(cfg), [])

    def test_rejects_mock_trade_id_for_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            bogus = {
                'order_ref': 500010,
                'instrument': 'SA609P900',
                'direction': '0',
                'offset': '0',
                'volume': 99,
                'trade_id': 'Q2',
                'trade_date': '20260611',
            }
            self.assertEqual(record_trades_to_cache([bogus], cfg), 0)
            self.assertEqual(load_cached_trades(cfg), [])

    def test_purge_removes_mock_entries_from_cache_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            path = cfg['dual_strategy']['trade_replay_cache']
            with open(path, 'w', encoding='utf-8') as f:
                f.write(json.dumps({
                    'order_ref': 50,
                    'instrument': 'IO2604-C-4000',
                    'trade_id': 'Q1',
                    'dedupe_key': 'IO2604-C-4000:Q1',
                    'trade_date': '20260611',
                }) + '\n')
                f.write(json.dumps({
                    'order_ref': 6,
                    'instrument': 'm2609-C-3000',
                    'trade_id': '103877',
                    'dedupe_key': 'M2609-C-3000:103877',
                    'trade_date': '20260611',
                }) + '\n')
            removed = purge_invalid_cache_entries(cfg)
            self.assertEqual(removed, 1)
            cached = load_cached_trades(cfg)
            self.assertEqual(len(cached), 1)
            self.assertEqual(cached[0]['trade_id'], '103877')


if __name__ == '__main__':
    unittest.main()
