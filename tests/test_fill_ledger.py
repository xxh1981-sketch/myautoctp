"""fill_ledger integration tests.

纯逻辑（fill_side、slippage、dedupe、原子 append）见 ``test_fill_ledger_unit.py``。
本文件保留 wire 链与 autotrade SPI 相关路径。
"""

import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ctp_bootstrap  # noqa: F401

from fill_ledger import wire_fill_ledger


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

        with tempfile.TemporaryDirectory() as tmp:
            conn.config = _cfg(tmp)
            handler = conn._runtime_state['_strangle_trade_handler']
            handler(conn, p_trade, None)
        self.assertEqual(called, ['prev'])


if __name__ == '__main__':
    unittest.main()
