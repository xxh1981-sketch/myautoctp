"""scripts/check_sensitive_files.py unit tests"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.check_sensitive_files import _is_blocked_path


class TestCheckSensitiveFiles(unittest.TestCase):

    def test_blocks_futuretrade_runtime(self):
        self.assertTrue(_is_blocked_path('futuretrade/execution_stats/combo_audit.jsonl'))

    def test_allows_data_example(self):
        self.assertFalse(_is_blocked_path('data/spread_positions.example.csv'))

    def test_blocks_data_runtime(self):
        self.assertTrue(_is_blocked_path('data/spread_positions.csv'))

    def test_allows_tradeinfo_example(self):
        self.assertFalse(_is_blocked_path('tradeinfo/spread.example.csv'))

    def test_blocks_tradeinfo_runtime(self):
        self.assertTrue(_is_blocked_path('tradeinfo/spread.csv'))

    def test_blocks_local_docs(self):
        self.assertTrue(_is_blocked_path('docs/LOCAL完整说明.md'))
        self.assertFalse(_is_blocked_path('docs/ARCHITECTURE.md'))

    def test_blocks_cursor_rules(self):
        self.assertTrue(_is_blocked_path('.cursor/rules/foo.mdc'))

    def test_blocks_merged_config(self):
        self.assertTrue(_is_blocked_path('merged_config.yaml'))
