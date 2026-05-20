"""strangle_positions.csv 自动维护单元测试"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from import_strangle_positions import (
    _fill_volume_delta,
    apply_fill_to_csv,
    load_positions_csv,
    save_positions_csv,
)
from pairtrade.constants import DIRECTION_BUY, DIRECTION_SELL, OFFSET_CLOSE, OFFSET_OPEN


class TestFillDelta(unittest.TestCase):

    def test_open_buy_positive(self):
        self.assertEqual(_fill_volume_delta(DIRECTION_BUY, OFFSET_OPEN, 2), 2)

    def test_close_sell_negative(self):
        self.assertEqual(_fill_volume_delta(DIRECTION_SELL, OFFSET_CLOSE, 3), -3)

    def test_other_zero(self):
        self.assertEqual(_fill_volume_delta(DIRECTION_SELL, OFFSET_OPEN, 1), 0)


class TestApplyFillToCsv(unittest.TestCase):

    def test_increment_and_decrement(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'pos.csv')
            cfg = {'dual_strategy': {'strangle_positions_csv': path}}
            save_positions_csv(path, {'SA609C1000': 1})
            claims = apply_fill_to_csv(
                cfg, 'SA609C1000', DIRECTION_BUY, OFFSET_OPEN, 1, None,
            )
            self.assertEqual(claims['SA609C1000'], 2)
            claims = apply_fill_to_csv(
                cfg, 'SA609C1000', DIRECTION_SELL, OFFSET_CLOSE, 2, None,
            )
            self.assertNotIn('SA609C1000', claims)
            self.assertEqual(load_positions_csv(path), {})


if __name__ == '__main__':
    unittest.main()
