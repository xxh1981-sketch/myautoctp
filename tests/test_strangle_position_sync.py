"""straggle_position_sync 行为（autostraggle 仓）。"""

import os
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ctp_bootstrap  # noqa: F401

from straggle_ledger import StrangleLedger
from straggle_position_sync import converge_flat_strangle_positions, query_ctp_long_volumes


class TestQueryCtpLongVolumes(unittest.TestCase):
    def test_query_failure_not_ok(self):
        conn = unittest.mock.MagicMock()
        conn.query_positions_sync.side_effect = TimeoutError('ctp')
        vols, ok = query_ctp_long_volumes(conn, {'C'})
        self.assertFalse(ok)
        self.assertEqual(vols, {})

    def test_query_none_not_ok(self):
        conn = unittest.mock.MagicMock()
        conn.query_positions_sync.return_value = None
        vols, ok = query_ctp_long_volumes(conn, {'C'})
        self.assertFalse(ok)

    def test_upper_trade_symbols_still_match_lower_prefix(self):
        """回归：过滤集大写时不得漏计 CTP 多头（曾导致假「双腿无仓」收敛）。"""
        conn = unittest.mock.MagicMock()
        conn.query_positions_sync.return_value = [
            {
                'instrument': 'c2701-C-2340',
                'direction': '2',
                'volume': 1,
            },
            {
                'instrument': 'c2701-P-2140',
                'PosiDirection': 2,
                'Position': 1,
            },
        ]
        vols, ok = query_ctp_long_volumes(conn, {'C'})
        self.assertTrue(ok)
        self.assertEqual(vols.get('C2701-C-2340'), 1)
        self.assertEqual(vols.get('C2701-P-2140'), 1)


class TestConvergeFlat(unittest.TestCase):
    def test_skips_converge_when_query_fails(self):
        conn = unittest.mock.MagicMock()
        conn.query_positions_sync.side_effect = RuntimeError('down')
        with tempfile.TemporaryDirectory() as td:
            ledger = StrangleLedger(os.path.join(td, 'l.json'))
            ledger.create_position(
                'c', '2701', 'c2701-C-2320', 'c2701-P-2120',
                2320.0, 2120.0, 0.01, groups=1,
            )
            n = converge_flat_strangle_positions(
                conn, ledger, 'c', '2701', {}, logger=None,
            )
            self.assertEqual(n, 0)
            open_pos = [
                p for p in ledger.list_positions('c', '2701')
                if p.get('status') == 'open'
            ]
            self.assertEqual(len(open_pos), 1)

    def test_does_not_converge_when_ctp_long_exists_upper_symbol(self):
        """process_strangle 传入大写品种符号时，仍不得误收敛真实多头。"""
        conn = unittest.mock.MagicMock()
        conn.query_positions_sync.return_value = [
            {'instrument': 'C2701-C-2340', 'direction': '2', 'volume': 1},
            {'instrument': 'C2701-P-2140', 'direction': '2', 'volume': 1},
        ]
        with tempfile.TemporaryDirectory() as td:
            ledger = StrangleLedger(os.path.join(td, 'l.json'))
            ledger.create_position(
                'c', '2701', 'C2701-C-2340', 'C2701-P-2140',
                2340.0, 2140.0, 0.01, groups=1,
            )
            n = converge_flat_strangle_positions(
                conn, ledger, 'C', '2701',
                {'strangle': {'post_close_cooldown_sec': 0}},
                logger=None,
            )
            self.assertEqual(n, 0)
            open_pos = [
                p for p in ledger.list_positions('c', '2701')
                if p.get('status') == 'open'
            ]
            self.assertEqual(len(open_pos), 1)
