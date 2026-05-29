"""trade_journal unit tests"""

import json
import os
import sys
import tempfile
import types
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if 'pairtrade.constants' not in sys.modules:
    _pt = types.ModuleType('pairtrade')
    _pt_const = types.ModuleType('pairtrade.constants')
    _pt_const.DIRECTION_BUY = '0'
    _pt_const.DIRECTION_SELL = '1'
    _pt_const.OFFSET_OPEN = '0'
    _pt_const.OFFSET_CLOSE = '1'
    _pt.constants = _pt_const
    sys.modules['pairtrade'] = _pt
    sys.modules['pairtrade.constants'] = _pt_const

from trade_journal import (
    active_journal_path,
    append_journal,
    journal_path_for_day,
    journal_retain_days,
    load_applied_keys,
    map_direction_offset,
    scan_unresolved_pending,
    trade_dedupe_key,
)


class TestTradeJournal(unittest.TestCase):

    def test_dedupe_key_prefers_trade_id(self):
        key = trade_dedupe_key({'trade_id': 'ABC', 'instrument': 'SA609C1000'})
        self.assertEqual(key, 'SA609C1000:ABC')

    def test_daily_shard_path(self):
        base = '/data/journal.jsonl'
        path = journal_path_for_day(base, date(2026, 5, 21))
        self.assertTrue(path.endswith('journal-20260521.jsonl'))

    def test_load_and_append_daily_shard(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = os.path.join(tmp, 'j.jsonl')
            cfg = {'dual_strategy': {'journal_daily_shards': True, 'journal_retain_days': 7}}
            append_journal(base, {'dedupe_key': 'k1'}, cfg)
            path = active_journal_path(base, cfg)
            self.assertTrue(os.path.isfile(path))
            keys = load_applied_keys(base, cfg)
            self.assertIn('k1', keys)
            with open(path, encoding='utf-8') as f:
                row = json.loads(f.read().strip())
            self.assertEqual(row['dedupe_key'], 'k1')

    def test_legacy_single_file_when_shards_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = os.path.join(tmp, 'j.jsonl')
            cfg = {'dual_strategy': {'journal_daily_shards': False}}
            append_journal(base, {'dedupe_key': 'legacy'}, cfg)
            self.assertTrue(os.path.isfile(base))
            self.assertIn('legacy', load_applied_keys(base, cfg))

    def test_fallback_dedupe_without_trade_id(self):
        key = trade_dedupe_key({
            'order_ref': 1,
            'instrument': 'sa609c1000',
            'direction': '0',
            'offset': '0',
            'volume': 2,
            'price': 50.5,
            'trade_date': '20260521',
            'trade_time': '09:30:00',
        })
        self.assertIn('SA609C1000', key)
        self.assertIn('50.5', key)

    def test_journal_retain_days_floor(self):
        self.assertGreaterEqual(journal_retain_days({'dual_strategy': {'journal_retain_days': 0}}), 1)

    def test_old_shards_excluded_from_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = os.path.join(tmp, 'j.jsonl')
            old_day = date.today() - timedelta(days=30)
            old_path = journal_path_for_day(base, old_day)
            with open(old_path, 'w', encoding='utf-8') as f:
                f.write(json.dumps({'dedupe_key': 'old'}) + '\n')
            cfg = {'dual_strategy': {'journal_daily_shards': True, 'journal_retain_days': 7}}
            append_journal(base, {'dedupe_key': 'recent'}, cfg)
            keys = load_applied_keys(base, cfg)
            self.assertIn('recent', keys)
            self.assertNotIn('old', keys)

    def test_load_applied_ignores_pending_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = os.path.join(tmp, 'j.jsonl')
            cfg = {'dual_strategy': {'journal_daily_shards': False}}
            append_journal(base, {'dedupe_key': 'k_pending', 'journal_state': 'pending'}, cfg)
            append_journal(base, {'dedupe_key': 'k_applied', 'journal_state': 'applied'}, cfg)
            keys = load_applied_keys(base, cfg)
            self.assertIn('k_applied', keys)
            self.assertNotIn('k_pending', keys)
            keys_with_pending = load_applied_keys(base, cfg, include_pending=True)
            self.assertIn('k_applied', keys_with_pending)
            self.assertIn('k_pending', keys_with_pending)

    def test_scan_unresolved_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = os.path.join(tmp, 'j.jsonl')
            cfg = {'dual_strategy': {'journal_daily_shards': False}}
            append_journal(base, {'dedupe_key': 'k1', 'journal_state': 'pending'}, cfg)
            append_journal(base, {'dedupe_key': 'k2', 'journal_state': 'pending'}, cfg)
            append_journal(base, {'dedupe_key': 'k2', 'journal_state': 'applied'}, cfg)
            stats = scan_unresolved_pending(base, cfg)
            self.assertEqual(stats['unresolved_pending'], 1)
            self.assertEqual(stats['malformed_lines'], 0)
            self.assertGreaterEqual(stats['total_lines'], 3)

    def test_unknown_state_counted_malformed_and_keeps_pending(self):
        """显式未知/拼写错误的 journal_state 不得当作 applied 清除 pending。"""
        with tempfile.TemporaryDirectory() as tmp:
            base = os.path.join(tmp, 'j.jsonl')
            cfg = {'dual_strategy': {'journal_daily_shards': False}}
            append_journal(base, {'dedupe_key': 'k1', 'journal_state': 'pending'}, cfg)
            # 同 key 的后续行状态拼写错误（'aplied'）→ 不应解除 pending。
            append_journal(base, {'dedupe_key': 'k1', 'journal_state': 'aplied'}, cfg)
            stats = scan_unresolved_pending(base, cfg)
            self.assertEqual(stats['unresolved_pending'], 1)
            self.assertEqual(stats['malformed_lines'], 1)
            # load_applied_keys 也不得把未知状态当成已应用键。
            keys = load_applied_keys(base, cfg, include_pending=False)
            self.assertNotIn('k1', keys)

    def test_legacy_missing_state_still_applied(self):
        """无 journal_state 字段的旧行仍按 applied 处理（向后兼容）。"""
        with tempfile.TemporaryDirectory() as tmp:
            base = os.path.join(tmp, 'j.jsonl')
            cfg = {'dual_strategy': {'journal_daily_shards': False}}
            append_journal(base, {'dedupe_key': 'legacy'}, cfg)
            stats = scan_unresolved_pending(base, cfg)
            self.assertEqual(stats['malformed_lines'], 0)
            self.assertIn('legacy', load_applied_keys(base, cfg))

    def test_map_direction_offset(self):
        d, o = map_direction_offset('0', '0')
        self.assertEqual((d, o), ('0', '0'))
        d, o = map_direction_offset('1', '1')
        self.assertEqual((d, o), ('1', '1'))


if __name__ == '__main__':
    unittest.main()
