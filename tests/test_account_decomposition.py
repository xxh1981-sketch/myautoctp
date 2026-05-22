"""account_decomposition unit tests."""

import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ctp_bootstrap  # noqa: F401

from account_decomposition import (
    compute_account_decomposition,
    external_explains_ctp_ahead,
    external_explains_strangle_gap,
    normalize_inst_map,
    register_acknowledged_external,
)


class FakeStore:
    def __init__(self, claims):
        self._claims = dict(claims)

    def list_leg_claims(self):
        return dict(self._claims)


class TestAccountDecomposition(unittest.TestCase):

    def _config(self):
        return {
            'spread_tradeinfo': [{'future': 'lc', 'month': '2609'}],
            'strangle_tradeinfo': [{'future': 'rm', 'month': '2609'}],
        }

    def test_normalize_inst_map_merges_case(self):
        m = normalize_inst_map({'lc2609-C-1': 1, 'LC2609-C-1': 2})
        self.assertEqual(m['LC2609-C-1'], 3)

    def test_balanced_spread_only(self):
        conn = MagicMock()
        conn.query_positions_sync.return_value = [
            {'instrument': 'lc2609-C-198000', 'direction': '2', 'position': 1},
            {'instrument': 'lc2609-C-228000', 'direction': '3', 'position': 2},
        ]
        conn._normalize_month = lambda sym, month: month
        ledger = MagicMock()
        ledger.list_leg_claims.return_value = {}
        ledger.list_unmatched_legs.return_value = []
        store = FakeStore({
            'lc2609-C-198000': 1,
            'lc2609-C-228000': -2,
        })
        result = compute_account_decomposition(
            conn, ledger, store, self._config(), None,
        )
        self.assertTrue(result['balanced'])
        self.assertEqual(result['external'], {})

    def test_case_insensitive_match(self):
        conn = MagicMock()
        conn.query_positions_sync.return_value = [
            {'instrument': 'LC2609-C-198000', 'direction': '2', 'position': 1},
        ]
        conn._normalize_month = lambda sym, month: month
        ledger = MagicMock()
        ledger.list_leg_claims.return_value = {}
        ledger.list_unmatched_legs.return_value = []
        store = FakeStore({'lc2609-C-198000': 1})
        result = compute_account_decomposition(
            conn, ledger, store, self._config(), None,
        )
        self.assertTrue(result['balanced'])

    def test_external_long_on_spread_month(self):
        conn = MagicMock()
        conn.query_positions_sync.return_value = [
            {'instrument': 'lc2609-C-198000', 'direction': '2', 'position': 2},
        ]
        conn._normalize_month = lambda sym, month: month
        ledger = MagicMock()
        ledger.list_leg_claims.return_value = {}
        ledger.list_unmatched_legs.return_value = []
        store = FakeStore({'lc2609-C-198000': 1})
        result = compute_account_decomposition(
            conn, ledger, store, self._config(), None,
        )
        self.assertFalse(result['balanced'])
        self.assertEqual(result['external'].get('LC2609-C-198000'), 1)

    def test_strangle_long_balanced(self):
        conn = MagicMock()
        conn.query_positions_sync.return_value = [
            {'instrument': 'RM609C2650', 'direction': '2', 'position': 1},
        ]
        conn._normalize_month = lambda sym, month: month
        ledger = MagicMock()
        ledger.list_leg_claims.return_value = {'RM609C2650': 1}
        ledger.list_unmatched_legs.return_value = []
        store = FakeStore({})
        result = compute_account_decomposition(
            conn, ledger, store, self._config(), None,
        )
        self.assertTrue(result['balanced'])

    def test_external_ack_explains_spread(self):
        cfg = {}
        register_acknowledged_external(
            cfg, {'LC2609-C-198000': 2}, persist=False,
        )
        self.assertTrue(
            external_explains_ctp_ahead('lc2609-C-198000', 3, 1, cfg)
        )
        self.assertFalse(
            external_explains_ctp_ahead('lc2609-C-198000', 3, 2, cfg)
        )

    def test_external_ack_explains_strangle_gap(self):
        cfg = {}
        register_acknowledged_external(cfg, {'RM609C2650': 1}, persist=False)
        self.assertTrue(external_explains_strangle_gap('rm609c2650', 1, cfg))
        self.assertFalse(external_explains_strangle_gap('rm609c2650', 2, cfg))


if __name__ == '__main__':
    unittest.main()
