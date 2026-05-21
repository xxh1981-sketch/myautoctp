"""data_path_guard unit tests"""

import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data_path_guard import (
    guard_repo_data_write,
    is_repo_data_path,
    repo_data_dir,
)


class TestDataPathGuard(unittest.TestCase):

    def test_is_repo_data_path(self):
        data = repo_data_dir()
        self.assertTrue(is_repo_data_path(os.path.join(data, 'spread_positions.csv')))
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(is_repo_data_path(os.path.join(tmp, 'spread.csv')))

    def test_blocks_write_under_data_during_pytest(self):
        data = repo_data_dir()
        target = os.path.join(data, '_pytest_guard_probe.txt')
        prev = os.environ.get('PYTEST_CURRENT_TEST')
        os.environ['PYTEST_CURRENT_TEST'] = 'tests/test_data_path_guard.py::probe'
        try:
            with self.assertRaises(RuntimeError):
                guard_repo_data_write(target)
        finally:
            if prev is None:
                os.environ.pop('PYTEST_CURRENT_TEST', None)
            else:
                os.environ['PYTEST_CURRENT_TEST'] = prev

    def test_atomic_io_respects_guard(self):
        from atomic_io import atomic_write_text
        data = repo_data_dir()
        target = os.path.join(data, '_pytest_atomic_probe.txt')
        prev = os.environ.get('PYTEST_CURRENT_TEST')
        os.environ['PYTEST_CURRENT_TEST'] = 'tests/test_data_path_guard.py::atomic'
        try:
            with self.assertRaises(RuntimeError):
                atomic_write_text(target, 'x')
        finally:
            if prev is None:
                os.environ.pop('PYTEST_CURRENT_TEST', None)
            else:
                os.environ['PYTEST_CURRENT_TEST'] = prev


    def test_wrong_csv_config_key_cannot_hit_production(self):
        """回归：误用 spread_positions_csv_path 时不得写入 repo data/。"""
        from spread_fill_sync import apply_spread_trade_record

        prev = os.environ.get('PYTEST_CURRENT_TEST')
        os.environ['PYTEST_CURRENT_TEST'] = (
            'tests/test_data_path_guard.py::wrong_key'
        )
        try:
            with tempfile.TemporaryDirectory() as tmp:
                cfg = {
                    'spread_tradeinfo': [{'future': 'SA', 'month': '609'}],
                    'dual_strategy': {
                        # 故意用错误键名 — 应回落到 data/spread_positions.csv 并被 guard 拦截
                        'spread_positions_csv_path': os.path.join(tmp, 'ignored.csv'),
                        'spread_fill_require_tradeinfo_match': False,
                    },
                }
                trade = {
                    'order_ref': 999,
                    'instrument': 'RM509-C-9000',
                    'direction': '0',
                    'offset': '0',
                    'volume': 1,
                    'trade_id': 'T_bad_key',
                    'trade_date': '20260521',
                    'trade_time': '12:00:00',
                }
                with self.assertRaises(RuntimeError):
                    apply_spread_trade_record(cfg, MagicMock(), trade, None)
        finally:
            if prev is None:
                os.environ.pop('PYTEST_CURRENT_TEST', None)
            else:
                os.environ['PYTEST_CURRENT_TEST'] = prev


if __name__ == '__main__':
    unittest.main()
