"""merged_tradeinfo CSV 校验单元测试。"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from merged_tradeinfo import _load_csv, load_dual_tradeinfo


class TestMergedTradeinfo(unittest.TestCase):

    def test_load_example_spread_csv(self):
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'tradeinfo',
            'spread.example.csv',
        )
        items = _load_csv(path)
        self.assertGreater(len(items), 0)
        self.assertEqual(items[0]['future'].lower(), 'ag')

    def test_rejects_duplicate_future_month(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'dup.csv')
            with open(path, 'w', encoding='utf-8') as f:
                f.write(
                    'future,month,vol_basis,vol_of_combo,min_tick\n'
                    'sa,2608,0.3,50,0.5\n'
                    'SA,2608,0.31,50,0.5\n'
                )
            with self.assertRaisesRegex(ValueError, '重复'):
                _load_csv(path)

    def test_rejects_missing_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'bad.csv')
            with open(path, 'w', encoding='utf-8') as f:
                f.write('future,month\nsa,2608\n')
            with self.assertRaisesRegex(ValueError, '缺少列'):
                _load_csv(path)

    def test_rejects_non_positive_params(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'bad.csv')
            with open(path, 'w', encoding='utf-8') as f:
                f.write(
                    'future,month,vol_basis,vol_of_combo,min_tick\n'
                    'sa,2608,0,50,0.5\n'
                )
            with self.assertRaisesRegex(ValueError, '必须为正数'):
                _load_csv(path)

    def test_load_dual_tradeinfo_from_csv_dir(self):
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        trade_dir = os.path.join(repo, 'tradeinfo')
        spread = os.path.join(trade_dir, 'spread.example.csv')
        strangle = os.path.join(trade_dir, 'strangle.example.csv')
        config = {
            'dual_strategy': {
                'tradeinfo_path': trade_dir,
                'spread_csv': spread,
                'strangle_csv': strangle,
            }
        }
        spread_items, strangle_items, combined = load_dual_tradeinfo(config)
        self.assertGreater(len(spread_items), 0)
        self.assertGreater(len(strangle_items), 0)
        self.assertGreaterEqual(len(combined), len(spread_items))


if __name__ == '__main__':
    unittest.main()
