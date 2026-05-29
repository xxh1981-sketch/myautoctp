"""spread_claims_guard tests."""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ctp_bootstrap  # noqa: F401

from spread_claims_guard import (
    audit_spread_claims,
    instrument_in_spread_tradeinfo,
    purge_invalid_spread_claims,
)
from spread_ledger import SpreadLegStore


class TestSpreadClaimsGuard(unittest.TestCase):
    def _conn(self):
        conn = MagicMock()
        conn._normalize_month = lambda sym, m: m
        return conn

    def test_rejects_rm509_not_in_ma_tradeinfo(self):
        conn = self._conn()
        spread_info = [{'future': 'MA', 'month': '609'}]
        self.assertFalse(
            instrument_in_spread_tradeinfo('RM509-C-9000', conn, spread_info),
        )
        self.assertTrue(
            instrument_in_spread_tradeinfo('MA609C2900', conn, spread_info),
        )

    def test_audit_flags_orphan_and_wrong_symbol(self):
        claims = {
            'MA609C2900': 1,
            'RM509-C-9000': 72,
            'SA609C1000': 12,
        }
        ctp = {
            'MA609C2900': 1,
            'MA609C2950': 5,
        }
        issues = audit_spread_claims(
            claims,
            [{'future': 'MA', 'month': '609'}, {'future': 'lc', 'month': '2609'}],
            conn=self._conn(),
            ctp_signed=ctp,
        )
        self.assertTrue(any('RM509' in i for i in issues))
        self.assertTrue(any('SA609C1000' in i for i in issues))


    def test_repair_journal_drops_rm609(self):
        import json
        import tempfile
        from spread_claims_guard import repair_spread_trade_journals

        with tempfile.TemporaryDirectory() as tmp:
            journal = os.path.join(tmp, 'spread_journal.jsonl')
            cfg = {
                'dual_strategy': {
                    'spread_trade_journal': journal,
                    'journal_daily_shards': False,
                },
                'spread_tradeinfo': [{'future': 'MA', 'month': '609'}],
            }
            with open(journal, 'w', encoding='utf-8') as f:
                f.write(json.dumps({
                    'dedupe_key': 'x',
                    'instrument': 'RM609C2650',
                    'order_ref': 8,
                }) + '\n')
                f.write(json.dumps({
                    'dedupe_key': 'y',
                    'instrument': 'MA609C2900',
                    'order_ref': 1,
                }) + '\n')
            removed, kept = repair_spread_trade_journals(cfg)
            self.assertEqual(removed, 1)
            self.assertEqual(kept, 1)
            with open(journal, 'r', encoding='utf-8') as f:
                lines = [ln for ln in f if ln.strip()]
            self.assertEqual(len(lines), 1)
            self.assertIn('MA609C2900', lines[0])

    def test_purge_orphan_and_wrong_month(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            cfg = {
                'dual_strategy': {
                    'spread_positions_csv': os.path.join(tmp, 'spread.csv'),
                    'spread_purge_invalid_claims_on_startup': True,
                },
                'spread_tradeinfo': [{'future': 'MA', 'month': '609'}],
            }
            path = cfg['dual_strategy']['spread_positions_csv']
            with open(path, 'w', encoding='utf-8') as f:
                f.write('instrument,volume\n')
                f.write('MA609C2900,1\n')
                f.write('RM509-C-9000,180\n')
                f.write('SA609C1000,30\n')

            store = SpreadLegStore()
            conn = self._conn()
            ctp = {'MA609C2900': 1}
            with patch('spread_derive.query_ctp_signed_positions', return_value=ctp):
                n = purge_invalid_spread_claims(
                    cfg, conn, cfg['spread_tradeinfo'], store=store, logger=None,
                )
            self.assertEqual(n, 2)
            self.assertEqual(store.list_leg_claims(), {'MA609C2900': 1})


if __name__ == '__main__':
    unittest.main()
