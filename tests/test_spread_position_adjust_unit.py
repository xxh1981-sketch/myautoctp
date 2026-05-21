"""spread_position_adjust 纯逻辑 unit tests。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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


class TestMergeStrangleOwnedVolumes(unittest.TestCase):

    def test_leg_claims_and_unmatched(self):
        ledger = FakeLedger(
            claims={'ma609c2900': 1},
            unmatched=[{
                'filled_instrument': 'SA609C2400',
                'volume': 2,
            }],
        )
        vols = merge_strangle_owned_volumes(ledger)
        self.assertEqual(vols['MA609C2900'], 1)
        self.assertEqual(vols['SA609C2400'], 2)

    def test_none_ledger_empty(self):
        self.assertEqual(merge_strangle_owned_volumes(None), {})


class TestExcludeStrangleFromPositions(unittest.TestCase):

    def test_subtracts_long_only(self):
        positions = [
            {'instrument': 'MA609C2900', 'direction': '2', 'position': 3},
            {'instrument': 'MA609C2950', 'direction': '3', 'position': 2},
        ]
        out = exclude_strangle_from_positions(
            positions, {'MA609C2900': 2}, symbol='MA',
        )
        self.assertEqual(len(out), 2)
        long_row = next(r for r in out if r['instrument'] == 'MA609C2900')
        self.assertEqual(long_row['position'], 1)
        short_row = next(r for r in out if r['instrument'] == 'MA609C2950')
        self.assertEqual(short_row['position'], 2)

    def test_fully_excluded_long_dropped(self):
        positions = [{'instrument': 'MA609C2900', 'direction': '2', 'position': 1}]
        out = exclude_strangle_from_positions(positions, {'MA609C2900': 5})
        self.assertEqual(out, [])


if __name__ == '__main__':
    unittest.main()
