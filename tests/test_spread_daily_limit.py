"""spread_daily_limit unit tests。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from spread_daily_limit import resolve_spread_daily_limit


class TestResolveSpreadDailyLimit(unittest.TestCase):

    def test_none_fc_blocks_open_conservatively(self):
        warnings = []
        filled, open_ok = resolve_spread_daily_limit(
            None, 5, True, log_warning=warnings.append,
        )
        self.assertEqual(filled, 5)
        self.assertFalse(open_ok)
        self.assertTrue(warnings)

    def test_under_limit_preserves_open_ok(self):
        filled, open_ok = resolve_spread_daily_limit(2, 5, True)
        self.assertEqual(filled, 2)
        self.assertTrue(open_ok)

    def test_at_limit_blocks_open(self):
        filled, open_ok = resolve_spread_daily_limit(5, 5, True)
        self.assertEqual(filled, 5)
        self.assertFalse(open_ok)

    def test_preserves_prior_open_ok_false(self):
        filled, open_ok = resolve_spread_daily_limit(1, 5, False)
        self.assertEqual(filled, 1)
        self.assertFalse(open_ok)


if __name__ == '__main__':
    unittest.main()
