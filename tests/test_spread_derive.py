"""spread_derive unit tests"""

import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ctp_bootstrap  # noqa: F401

from spread_derive import apply_derived_spread_from_ctp, derive_spread_claims_from_ctp
from spread_ledger import SpreadLegStore


class FakeLedger:
    def __init__(self, claims=None, unmatched=None):
        self._claims = claims or {}
        self._unmatched = unmatched or []

    def list_leg_claims(self):
        return dict(self._claims)

    def list_unmatched_legs(self):
        return list(self._unmatched)


class TestSpreadDerive(unittest.TestCase):

    def test_ctp_minus_strangle_long_calls(self):
        conn = MagicMock()
        conn.query_positions_sync.return_value = [
            {'instrument': 'MA609C2900', 'direction': '2', 'position': 2},
            {'instrument': 'MA609C2950', 'direction': '3', 'position': 4},
            {'instrument': 'SA609P900', 'direction': '2', 'position': 1},
        ]
        ledger = FakeLedger(claims={'MA609C2900': 1, 'SA609P900': 1})
        claims, _ = derive_spread_claims_from_ctp(conn, ledger)
        self.assertEqual(claims['MA609C2900'], 1)
        self.assertEqual(claims['MA609C2950'], -4)
        self.assertNotIn('SA609P900', claims)

    def test_spread_only_from_ctp(self):
        conn = MagicMock()
        conn.query_positions_sync.return_value = [
            {'instrument': 'MA609C2900', 'direction': '2', 'position': 1},
            {'instrument': 'MA609C2950', 'direction': '3', 'position': 2},
        ]
        ledger = FakeLedger()
        claims, _ = derive_spread_claims_from_ctp(conn, ledger)
        self.assertEqual(claims['MA609C2900'], 1)
        self.assertEqual(claims['MA609C2950'], -2)

    def test_query_failure(self):
        conn = MagicMock()
        conn.query_positions_sync.return_value = None
        claims, msg = derive_spread_claims_from_ctp(conn, FakeLedger())
        self.assertIsNone(claims)
        self.assertIn('失败', msg)

    def test_apply_derived_reloads_store_from_csv(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = os.path.join(tmp, 'spread.csv')
            cfg = {'dual_strategy': {'spread_positions_csv': csv_path}}
            conn = MagicMock()
            conn.query_positions_sync.return_value = [
                {'instrument': 'MA609C2900', 'direction': '2', 'position': 1},
            ]
            store = SpreadLegStore()
            with unittest.mock.patch(
                'spread_derive.sync_spread_leg_claims',
            ) as mock_sync:
                out = apply_derived_spread_from_ctp(
                    conn, FakeLedger(), store, cfg, logger=None,
                )
            self.assertEqual(out, {'MA609C2900': 1})
            mock_sync.assert_called_once_with(store, cfg, logger=None)
            self.assertTrue(os.path.isfile(csv_path))


if __name__ == '__main__':
    unittest.main()
