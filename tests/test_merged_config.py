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

    def test_defaults_include_reconcile_diagnostics(self):
        self.assertTrue(DUAL_STRATEGY_DEFAULTS.get('reconcile_diagnostic_snapshot_enabled'))
        self.assertEqual(DUAL_STRATEGY_DEFAULTS.get('reconcile_diagnostic_issue_limit'), 8)

    def test_defaults_include_ctp_unknown_direction_warn(self):
        self.assertTrue(DUAL_STRATEGY_DEFAULTS.get('ctp_unknown_direction_warn'))

    def test_defaults_include_store_fallback_and_orderref_detector(self):
        self.assertEqual(DUAL_STRATEGY_DEFAULTS.get('strangle_store_unavailable_fallback'), 'skip_fill')
        self.assertTrue(DUAL_STRATEGY_DEFAULTS.get('orderref_bilateral_nonzero_alert'))

    def test_defaults_unattended_startup_ack(self):
        self.assertFalse(DUAL_STRATEGY_DEFAULTS.get('startup_ack_each_run'))
        self.assertTrue(DUAL_STRATEGY_DEFAULTS.get('startup_ack_persist'))
        self.assertFalse(DUAL_STRATEGY_DEFAULTS.get('startup_ack_require_today'))
        self.assertTrue(DUAL_STRATEGY_DEFAULTS.get('startup_ack_use_gui'))
        self.assertTrue(DUAL_STRATEGY_DEFAULTS.get('startup_ack_prefer_gui'))

    def test_merged_top_level_defaults(self):
        self.assertEqual(MERGED_TOP_LEVEL_DEFAULTS['global_margin_limit'], 100000)
        self.assertEqual(MERGED_TOP_LEVEL_DEFAULTS['main_loop_max_consecutive_errors'], 10)
        self.assertTrue(MERGED_TOP_LEVEL_DEFAULTS['fail_fast_on_guard_install'])
        self.assertTrue(MERGED_TOP_LEVEL_DEFAULTS['fail_fast_on_empty_target_months'])
        self.assertTrue(MERGED_TOP_LEVEL_DEFAULTS['block_start_without_margin_limit'])

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

    def test_pause_spread_reconcile_false_warning(self):
        cfg = {
            'dual_strategy': {
                'strategy_order': ['spread'],
                'pause_spread_open_on_reconcile_mismatch': False,
            },
            'strangle': {'order_ref_min': 500000},
        }
        errors, warnings = _validate_merged_config(cfg)
        self.assertEqual(errors, [])
        self.assertTrue(
            any('pause_spread_open_on_reconcile_mismatch=false' in w for w in warnings)
        )

    def test_pause_strangle_reconcile_false_warning(self):
        cfg = {
            'dual_strategy': {'strategy_order': ['spread']},
            'strangle': {
                'order_ref_min': 500000,
                'pause_open_on_reconcile_mismatch': False,
            },
        }
        errors, warnings = _validate_merged_config(cfg)
        self.assertEqual(errors, [])
        self.assertTrue(
            any('pause_open_on_reconcile_mismatch=false' in w for w in warnings)
        )

    def test_margin_zero_warning(self):
        cfg = {
            'dual_strategy': {'strategy_order': ['spread']},
            'strangle': {'order_ref_min': 500000},
            'global_margin_limit': 0,
            'block_start_without_margin_limit': False,
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

    def test_new_reconcile_diagnostics_config_validation(self):
        cfg = {
            'dual_strategy': {
                'strategy_order': ['spread'],
                'reconcile_diagnostic_snapshot_enabled': 'yes',
                'reconcile_diagnostic_issue_limit': -1,
                'ctp_unknown_direction_warn': 'yes',
                'strangle_store_unavailable_fallback': 'bad',
                'orderref_bilateral_nonzero_alert': 'yes',
                'orderref_bilateral_nonzero_alert_cooldown_sec': -1,
            },
            'strangle': {
                'order_ref_min': 500000,
                'unmatched_leg_metadata_alert': 'yes',
                'unmatched_leg_metadata_alert_cooldown_sec': -1,
            },
        }
        errors, warnings = _validate_merged_config(cfg)
        self.assertTrue(any('reconcile_diagnostic_snapshot_enabled' in e for e in errors))
        self.assertTrue(any('reconcile_diagnostic_issue_limit' in e for e in errors))
        self.assertTrue(any('ctp_unknown_direction_warn' in e for e in errors))
        self.assertTrue(any('strangle_store_unavailable_fallback' in e for e in errors))
        self.assertTrue(any('orderref_bilateral_nonzero_alert' in e for e in errors))
        self.assertTrue(any('unmatched_leg_metadata_alert' in e for e in errors))
        self.assertEqual(warnings, [])

    def test_store_unavailable_fallback_allow_warns(self):
        cfg = {
            'dual_strategy': {
                'strategy_order': ['spread'],
                'strangle_store_unavailable_fallback': 'allow',
            },
            'strangle': {'order_ref_min': 500000},
        }
        errors, warnings = _validate_merged_config(cfg)
        self.assertEqual(errors, [])
        self.assertTrue(any('strangle_store_unavailable_fallback=allow' in w for w in warnings))

    def test_store_unavailable_fallback_list_is_validation_error(self):
        cfg = {
            'dual_strategy': {
                'strategy_order': ['spread'],
                'strangle_store_unavailable_fallback': ['allow'],
            },
            'strangle': {'order_ref_min': 500000},
        }
        errors, _warnings = _validate_merged_config(cfg)
        self.assertTrue(any('strangle_store_unavailable_fallback' in e for e in errors))


if __name__ == '__main__':
    unittest.main()
