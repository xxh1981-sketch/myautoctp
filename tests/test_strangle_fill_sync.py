"""宽跨成交入账 / 回放单元测试"""

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ctp_bootstrap  # noqa: F401

from pairtrade.constants import DIRECTION_BUY, OFFSET_OPEN
from strangle_fill_sync import (
    apply_strangle_trade_record,
    sync_csv_from_strangle_trades,
    wire_strangle_trade_runtime,
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
            'journal_daily_shards': False,
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
            applied = [
                json.loads(line) for line in lines
                if json.loads(line).get('journal_state') == 'applied'
            ]
            self.assertEqual(len(applied), 1)
            self.assertEqual(applied[0]['order_ref'], 500010)


class TestWireStrangleTradeRuntime(unittest.TestCase):
    """wire_strangle_trade_runtime must chain any pre-existing handler so a
    later wire ordering change (or a third party hooking in) does not silently
    drop spread/fill_ledger callbacks."""

    def _stub_p_trade(self, order_ref='100'):
        p = MagicMock()
        p.OrderRef = order_ref
        p.InstrumentID = b'SA609C1000'
        p.Direction = b'0'
        p.OffsetFlag = b'0'
        p.Volume = 1
        p.Price = 50.0
        p.TradeID = b''
        p.TradeDate = b''
        p.TradeTime = b''
        return p

    def test_chains_existing_handler(self):
        conn = MagicMock()
        conn._runtime_state = {}
        conn.config = {}

        called = []

        def prev(c, p, l):
            called.append('prev')

        conn._runtime_state['_unified_trade_handler'] = prev
        conn._runtime_state['_strangle_trade_handler'] = prev

        wire_strangle_trade_runtime(conn, MagicMock())

        handler = conn._runtime_state['_strangle_trade_handler']
        handler(conn, self._stub_p_trade(), None)

        self.assertEqual(called, ['prev'])

    def test_no_prev_handler_still_works(self):
        conn = MagicMock()
        conn._runtime_state = {}
        conn.config = {}
        wire_strangle_trade_runtime(conn, MagicMock())
        handler = conn._runtime_state['_strangle_trade_handler']
        handler(conn, self._stub_p_trade(), None)


if __name__ == '__main__':
    unittest.main()
