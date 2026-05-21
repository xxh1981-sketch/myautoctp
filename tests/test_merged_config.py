"""merged_config validation tests"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from merged_config import (
    DUAL_STRATEGY_DEFAULTS,
    MERGED_TOP_LEVEL_DEFAULTS,
    _validate_merged_config,
)


class TestMergedConfigValidation(unittest.TestCase):

    def test_defaults_include_journal_shards(self):
        self.assertTrue(DUAL_STRATEGY_DEFAULTS.get('journal_daily_shards'))

    def test_defaults_unattended_startup_ack(self):
        self.assertFalse(DUAL_STRATEGY_DEFAULTS.get('startup_ack_each_run'))
        self.assertTrue(DUAL_STRATEGY_DEFAULTS.get('startup_ack_persist'))
        self.assertFalse(DUAL_STRATEGY_DEFAULTS.get('startup_ack_require_today'))

    def test_merged_top_level_defaults(self):
        self.assertEqual(MERGED_TOP_LEVEL_DEFAULTS['global_margin_limit'], 100000)
        self.assertEqual(MERGED_TOP_LEVEL_DEFAULTS['main_loop_max_consecutive_errors'], 10)

    def test_invalid_strategy_order(self):
        cfg = {
            'dual_strategy': {'strategy_order': ['bad']},
            'strangle': {'order_ref_min': 500000},
        }
        errors, _warnings = _validate_merged_config(cfg)
        self.assertTrue(any('strategy_order' in e for e in errors))

    def test_order_ref_conflict(self):
        cfg = {
            'dual_strategy': {'spread_order_ref_max': 600000},
            'strangle': {'order_ref_min': 500000},
        }
        errors, _warnings = _validate_merged_config(cfg)
        self.assertTrue(any('OrderRef' in e for e in errors))

    def test_valid_minimal(self):
        cfg = {
            'dual_strategy': {'strategy_order': ['spread', 'strangle']},
            'strangle': {'order_ref_min': 500000},
        }
        errors, warnings = _validate_merged_config(cfg)
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])

    def test_margin_zero_warning(self):
        cfg = {
            'dual_strategy': {'strategy_order': ['spread']},
            'strangle': {'order_ref_min': 500000},
            'global_margin_limit': 0,
        }
        errors, warnings = _validate_merged_config(cfg)
        self.assertEqual(errors, [])
        self.assertTrue(any('global_margin_limit=0' in w for w in warnings))

    def test_margin_zero_block_start(self):
        cfg = {
            'dual_strategy': {'strategy_order': ['spread']},
            'strangle': {'order_ref_min': 500000},
            'global_margin_limit': 0,
            'block_start_without_margin_limit': True,
        }
        errors, _warnings = _validate_merged_config(cfg)
        self.assertTrue(any('block_start' in e for e in errors))


if __name__ == '__main__':
    unittest.main()
