"""spread_position_adjust unit tests"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ctp_bootstrap  # noqa: F401

from spread_position_adjust import (
    exclude_strangle_from_positions,
    merge_strangle_owned_volumes,
)


class FakeLedger:
    def __init__(self, claims=None, unmatched=None):
        self._claims = claims or {}
        self._unmatched = unmatched or []

    def list_leg_claims(self):
        return dict(self._claims)

    def list_unmatched_legs(self):
        return list(self._unmatched)


class TestSpreadPositionAdjust(unittest.TestCase):

    def test_merge_claims_and_unmatched(self):
        ledger = FakeLedger(
            claims={'SA609C1000': 2},
            unmatched=[{'filled_instrument': 'SA609P900', 'volume': 1}],
        )
        vols = merge_strangle_owned_volumes(ledger)
        self.assertEqual(vols['SA609C1000'], 2)
        self.assertEqual(vols['SA609P900'], 1)

    def test_exclude_long_call_for_spread_a(self):
        positions = [
            {'instrument': 'SA609C1000', 'direction': '2', 'position': 3},
            {'instrument': 'SA609C1100', 'direction': '2', 'position': 2},
            {'instrument': 'SA609C1200', 'direction': '3', 'position': 4},
        ]
        out = exclude_strangle_from_positions(
            positions, {'SA609C1000': 2}, symbol='SA',
        )
        by_inst = {p['instrument']: p['position'] for p in out}
        self.assertEqual(by_inst['SA609C1000'], 1)
        self.assertEqual(by_inst['SA609C1100'], 2)
        self.assertEqual(by_inst['SA609C1200'], 4)

    def test_strangle_only_leaves_spread_empty_for_that_leg(self):
        positions = [{'instrument': 'lc2609-C-198000', 'direction': '2', 'position': 1}]
        out = exclude_strangle_from_positions(
            positions, {'LC2609-C-198000': 1}, symbol='lc',
        )
        self.assertEqual(out, [])


if __name__ == '__main__':
    unittest.main()
