"""spread_ledger / import_spread_positions unit tests"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ctp_bootstrap  # noqa: F401

from import_spread_positions import (
    apply_fill_to_spread_csv,
    load_spread_positions_csv,
    save_spread_positions_csv,
    spread_fill_delta,
)
from pairtrade.constants import DIRECTION_BUY, DIRECTION_SELL, OFFSET_CLOSE, OFFSET_OPEN
from spread_ledger import SpreadLegStore


class TestSpreadLedger(unittest.TestCase):

    def test_long_call_volumes(self):
        store = SpreadLegStore()
        store.set_leg_claims({'MA609C2900': 1, 'MA609C2950': -2, 'MA609P2700': 1})
        self.assertEqual(store.long_call_volumes(), {'MA609C2900': 1})
        self.assertEqual(store.short_call_volumes(), {'MA609C2950': 2})

    def test_fill_delta_signed(self):
        self.assertEqual(spread_fill_delta(DIRECTION_BUY, OFFSET_OPEN, 1), 1)
        self.assertEqual(spread_fill_delta(DIRECTION_SELL, OFFSET_OPEN, 2), -2)
        self.assertEqual(spread_fill_delta(DIRECTION_SELL, OFFSET_CLOSE, 1), -1)
        self.assertEqual(spread_fill_delta(DIRECTION_BUY, OFFSET_CLOSE, 1), 1)


class TestSpreadCsv(unittest.TestCase):

    def test_round_trip_signed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'spread.csv')
            save_spread_positions_csv(path, {'MA609C2900': 1, 'MA609C2950': -2})
            claims = load_spread_positions_csv(path)
            self.assertEqual(claims['MA609C2900'], 1)
            self.assertEqual(claims['MA609C2950'], -2)

    def test_apply_fill_to_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = {'dual_strategy': {'spread_positions_csv': os.path.join(tmp, 's.csv')}}
            claims = apply_fill_to_spread_csv(
                cfg, 'MA609C2900', DIRECTION_BUY, OFFSET_OPEN, 1,
            )
            self.assertEqual(claims['MA609C2900'], 1)
            claims = apply_fill_to_spread_csv(
                cfg, 'MA609C2900', DIRECTION_SELL, OFFSET_CLOSE, 1,
            )
            self.assertNotIn('MA609C2900', claims)


if __name__ == '__main__':
    unittest.main()
