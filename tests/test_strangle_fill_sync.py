"""宽跨成交入账 / 回放单元测试"""

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ctp_bootstrap  # noqa: F401

from pairtrade.constants import DIRECTION_BUY, DIRECTION_SELL, OFFSET_CLOSE, OFFSET_OPEN
from strangle_fill_sync import (
    apply_strangle_trade_record,
    sync_csv_from_strangle_trades,
    _trade_dedupe_key,
)


def _cfg(tmp, journal_name='journal.jsonl'):
    csv_path = os.path.join(tmp, 'pos.csv')
    journal = os.path.join(tmp, journal_name)
    return {
        'strangle': {'order_ref_min': 500000},
        'dual_strategy': {
            'spread_order_ref_max': 499999,
            'strangle_positions_csv': csv_path,
            'strangle_trade_journal': journal,
        },
    }


class TestStrangleFillSync(unittest.TestCase):

    def test_spread_trade_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            ledger = MagicMock()
            ok = apply_strangle_trade_record(cfg, ledger, {
                'order_ref': 100,
                'instrument': 'SA609C1000',
                'direction': '0',
                'offset': '0',
                'volume': 1,
                'trade_id': 'T1',
            })
            self.assertFalse(ok)
            ledger.set_leg_claims.assert_not_called()

    def test_strangle_trade_updates_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            ledger = MagicMock()
            trade = {
                'order_ref': 500001,
                'instrument': 'SA609C1000',
                'direction': DIRECTION_BUY,
                'offset': OFFSET_OPEN,
                'volume': 2,
                'trade_id': 'T2',
            }
            self.assertTrue(apply_strangle_trade_record(cfg, ledger, trade))
            self.assertTrue(apply_strangle_trade_record(cfg, ledger, trade) is False)
            ledger.set_leg_claims.assert_called_once()

    def test_sync_from_query_replays_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            ledger = MagicMock()
            conn = MagicMock()
            conn.query_trades_sync.return_value = [
                {
                    'order_ref': 500010,
                    'instrument': 'SA609P900',
                    'direction': '0',
                    'offset': '0',
                    'volume': 1,
                    'price': 50.0,
                    'trade_id': 'Q1',
                    'trade_date': '20260520',
                    'trade_time': '10:00:00',
                },
                {
                    'order_ref': 50,
                    'instrument': 'SA609C1000',
                    'direction': '0',
                    'offset': '0',
                    'volume': 99,
                    'trade_id': 'Q2',
                },
            ]
            n = sync_csv_from_strangle_trades(conn, ledger, cfg, logger=None)
            self.assertEqual(n, 1)
            journal = cfg['dual_strategy']['strangle_trade_journal']
            lines = open(journal, encoding='utf-8').read().strip().splitlines()
            self.assertEqual(len(lines), 1)
            row = json.loads(lines[0])
            self.assertEqual(row['order_ref'], 500010)


if __name__ == '__main__':
    unittest.main()
