"""strangle_positions.csv 读写与增量维护单元测试（不 import autotrade）。"""

import os
import sys
import tempfile
import types
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# _fill_volume_delta 延迟 import pairtrade.constants；CI 无 autotrade 时 stub。
if 'pairtrade.constants' not in sys.modules:
    _pt = types.ModuleType('pairtrade')
    _pt_const = types.ModuleType('pairtrade.constants')
    _pt_const.DIRECTION_BUY = '0'
    _pt_const.DIRECTION_SELL = '1'
    _pt_const.OFFSET_OPEN = '0'
    _pt_const.OFFSET_CLOSE = '1'
    _pt.constants = _pt_const
    sys.modules['pairtrade'] = _pt
    sys.modules['pairtrade.constants'] = _pt_const

from import_strangle_positions import (
    _fill_volume_delta,
    _looks_like_header,
    apply_fill_to_csv,
    load_positions_csv,
    positions_csv_path,
    save_positions_csv,
    sync_strangle_leg_claims,
)

# 与 pairtrade.constants 一致，避免 unit CI 依赖 autotrade 路径
DIRECTION_BUY = '0'
DIRECTION_SELL = '1'
OFFSET_OPEN = '0'
OFFSET_CLOSE = '1'


class TestLooksLikeHeader(unittest.TestCase):

    def test_english_header(self):
        self.assertTrue(_looks_like_header(['instrument', 'volume']))

    def test_chinese_header(self):
        self.assertTrue(_looks_like_header(['期权代码', '持仓手数']))

    def test_data_row_not_header(self):
        self.assertFalse(_looks_like_header(['SA609C1000', '2']))


class TestLoadPositionsCsv(unittest.TestCase):

    def test_empty_file_returns_empty(self):
        with tempfile.NamedTemporaryFile('w', suffix='.csv', delete=False, encoding='utf-8') as f:
            f.write('')
            path = f.name
        try:
            self.assertEqual(load_positions_csv(path), {})
        finally:
            os.remove(path)

    def test_header_only_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'pos.csv')
            with open(path, 'w', encoding='utf-8-sig') as f:
                f.write('instrument,volume\n')
            self.assertEqual(load_positions_csv(path), {})

    def test_chinese_header_and_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'pos.csv')
            with open(path, 'w', encoding='utf-8') as f:
                f.write('期权代码,持仓手数\nSA609C1000,2\n')
            self.assertEqual(load_positions_csv(path), {'SA609C1000': 2})

    def test_duplicate_rows_sum_volume(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'pos.csv')
            with open(path, 'w', encoding='utf-8') as f:
                f.write('SA609C1000,1\nSA609C1000,2\n')
            self.assertEqual(load_positions_csv(path), {'SA609C1000': 3})

    def test_rejects_short_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'bad.csv')
            with open(path, 'w', encoding='utf-8') as f:
                f.write('only_one_col\n')
            with self.assertRaisesRegex(ValueError, 'expected 2 columns'):
                load_positions_csv(path)

    def test_rejects_empty_instrument(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'bad.csv')
            with open(path, 'w', encoding='utf-8') as f:
                f.write(',1\n')
            with self.assertRaisesRegex(ValueError, 'instrument is empty'):
                load_positions_csv(path)

    def test_rejects_non_positive_volume(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'bad.csv')
            with open(path, 'w', encoding='utf-8') as f:
                f.write('SA609C1000,0\n')
            with self.assertRaisesRegex(ValueError, 'positive integer'):
                load_positions_csv(path)


class TestSavePositionsCsv(unittest.TestCase):

    def test_sorted_output_and_skips_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'pos.csv')
            save_positions_csv(path, {'ZZ609C9000': 1, 'AA609C8000': 2, 'gone': 0})
            with open(path, encoding='utf-8') as f:
                text = f.read()
            self.assertIn('instrument,volume', text.splitlines()[0])
            lines = [ln for ln in text.splitlines()[1:] if ln.strip()]
            self.assertEqual(lines, ['AA609C8000,2', 'ZZ609C9000,1'])


class TestPositionsCsvPath(unittest.TestCase):

    def test_relative_path_under_project(self):
        cfg = {'dual_strategy': {'strangle_positions_csv': 'data/custom.csv'}}
        path = positions_csv_path(cfg)
        self.assertTrue(path.replace('\\', '/').endswith('/data/custom.csv'))

    def test_absolute_path_unchanged(self):
        cfg = {'dual_strategy': {'strangle_positions_csv': 'C:/tmp/pos.csv'}}
        self.assertEqual(positions_csv_path(cfg), 'C:/tmp/pos.csv')


class TestFillDelta(unittest.TestCase):

    def test_open_buy_positive(self):
        self.assertEqual(_fill_volume_delta(DIRECTION_BUY, OFFSET_OPEN, 2), 2)

    def test_close_sell_negative(self):
        self.assertEqual(_fill_volume_delta(DIRECTION_SELL, OFFSET_CLOSE, 3), -3)

    def test_other_zero(self):
        self.assertEqual(_fill_volume_delta(DIRECTION_SELL, OFFSET_OPEN, 1), 0)

    def test_non_positive_traded_zero(self):
        self.assertEqual(_fill_volume_delta(DIRECTION_BUY, OFFSET_OPEN, 0), 0)


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

    def test_zero_delta_leaves_csv_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'pos.csv')
            cfg = {'dual_strategy': {'strangle_positions_csv': path}}
            save_positions_csv(path, {'SA609C1000': 1})
            mtime_before = os.path.getmtime(path)
            claims = apply_fill_to_csv(
                cfg, 'SA609C1000', DIRECTION_SELL, OFFSET_OPEN, 1, None,
            )
            self.assertEqual(claims, {'SA609C1000': 1})
            self.assertEqual(os.path.getmtime(path), mtime_before)


class TestSyncStrangleLegClaims(unittest.TestCase):

    def test_missing_file_clears_claims(self):
        ledger = MagicMock()
        cfg = {'dual_strategy': {'strangle_positions_csv': '/nonexistent/path.csv'}}
        n = sync_strangle_leg_claims(ledger, cfg, logger=None)
        self.assertEqual(n, 0)
        ledger.set_leg_claims.assert_called_once_with({})

    def test_populated_csv_syncs_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'pos.csv')
            save_positions_csv(path, {'SA609C1000': 1, 'SA609P900': 2})
            ledger = MagicMock()
            n = sync_strangle_leg_claims(ledger, csv_path=path, logger=None)
            self.assertEqual(n, 2)
            ledger.set_leg_claims.assert_called_once()
            claims = ledger.set_leg_claims.call_args[0][0]
            self.assertEqual(claims['SA609C1000'], 1)
            self.assertEqual(claims['SA609P900'], 2)


if __name__ == '__main__':
    unittest.main()
