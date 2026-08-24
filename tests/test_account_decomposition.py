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
    external_explains_reconcile_gap,
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

    def test_normalize_trade_symbols_lower(self):
        from account_decomposition import normalize_trade_symbols
        self.assertEqual(normalize_trade_symbols({'C', 'sa', ' M '}), {'c', 'sa', 'm'})
        self.assertEqual(normalize_trade_symbols(None), set())
        self.assertEqual(normalize_trade_symbols([]), set())

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

    def test_awaiting_phase2_does_not_inflate_strangle_claim(self):
        conn = MagicMock()
        conn.query_positions_sync.return_value = [
            {'instrument': 'RM609C2750', 'direction': '2', 'position': 29},
        ]
        conn._normalize_month = lambda sym, month: month
        ledger = MagicMock()
        ledger.list_leg_claims.return_value = {'RM609C2750': 29}
        ledger.list_unmatched_legs.return_value = [{
            'kind': 'awaiting_phase2',
            'filled_instrument': 'RM609C2750',
            'leg': {'inst': 'RM609P2025'},
            'volume': 8,
        }]
        store = FakeStore({})
        result = compute_account_decomposition(
            conn, ledger, store, self._config(), None,
        )
        self.assertTrue(result['balanced'])
        self.assertEqual(result['external'], {})

    def test_inferred_single_from_claims_does_not_inflate_strangle_claim(self):
        conn = MagicMock()
        conn.query_positions_sync.return_value = [
            {'instrument': 'MA609P1900', 'direction': '2', 'position': 42},
            {'instrument': 'MA609C3650', 'direction': '2', 'position': 35},
        ]
        conn._normalize_month = lambda sym, month: month
        cfg = {
            'spread_tradeinfo': [],
            'strangle_tradeinfo': [{'future': 'ma', 'month': '2609'}],
        }
        ledger = MagicMock()
        ledger.list_leg_claims.return_value = {
            'MA609P1900': 42,
            'MA609C3650': 35,
        }
        ledger.list_unmatched_legs.return_value = [{
            'kind': 'inferred_single',
            'leg': {'inst': 'MA609P1900', 'label': 'put'},
            'volume': 7,
            'inferred_from_claims': True,
        }]
        store = FakeStore({})
        result = compute_account_decomposition(
            conn, ledger, store, cfg, None,
        )
        self.assertTrue(result['balanced'])
        self.assertEqual(result['external'], {})

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

    def test_external_ack_explains_csv_ahead_negative(self):
        cfg = {}
        register_acknowledged_external(
            cfg, {'LC2609-C-198000': -5}, persist=False,
        )
        self.assertTrue(
            external_explains_reconcile_gap('lc2609-C-198000', 0, 5, cfg),
        )
        self.assertFalse(
            external_explains_reconcile_gap('lc2609-C-198000', 0, 4, cfg),
        )

    def test_external_ack_explains_short_ctp_ahead(self):
        cfg = {}
        register_acknowledged_external(
            cfg, {'LC2609-C-228000': -2}, persist=False,
        )
        self.assertTrue(
            external_explains_ctp_ahead('LC2609-C-228000', -5, -3, cfg),
        )


if __name__ == '__main__':
    unittest.main()
