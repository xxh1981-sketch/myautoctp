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

    def test_awaiting_phase2_filled_not_double_counted(self):
        """Phase1 已入 CSV 时，awaiting_phase2 的 filled_instrument 不再叠加。"""
        ledger = FakeLedger(
            claims={'RM609C2750': 29},
            unmatched=[{
                'kind': 'awaiting_phase2',
                'filled_instrument': 'RM609C2750',
                'leg': {'inst': 'RM609P2025'},
                'volume': 8,
            }],
        )
        vols = merge_strangle_owned_volumes(ledger)
        self.assertEqual(vols['RM609C2750'], 29)
        self.assertNotIn('RM609P2025', vols)

    def test_inferred_single_from_claims_not_double_counted(self):
        """inferred_single 来自 leg_claims 推断时，不得与 CSV 重复计入。"""
        ledger = FakeLedger(
            claims={'MA609P1900': 42, 'MA609C3650': 35},
            unmatched=[{
                'kind': 'inferred_single',
                'leg': {'inst': 'MA609P1900', 'label': 'put'},
                'volume': 7,
                'inferred_from_claims': True,
            }],
        )
        vols = merge_strangle_owned_volumes(ledger)
        self.assertEqual(vols['MA609P1900'], 42)
        self.assertEqual(vols['MA609C3650'], 35)

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
