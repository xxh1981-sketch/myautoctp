"""resolve_manual_start 单元测试。"""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from env_utils import is_config_abs_path, resolve_manual_start


class TestIsConfigAbsPath(unittest.TestCase):

    def test_windows_drive_path_on_any_platform(self):
        self.assertTrue(is_config_abs_path('C:/tmp/pos.csv'))
        self.assertTrue(is_config_abs_path(r'D:\data\x.csv'))

    def test_relative_path(self):
        self.assertFalse(is_config_abs_path('data/custom.csv'))

    def test_native_absolute(self):
        self.assertTrue(is_config_abs_path(os.path.abspath('x.csv')))


class TestResolveManualStart(unittest.TestCase):

    def test_manual_flag(self):
        with patch('env_utils.argv_has', return_value=True):
            self.assertTrue(resolve_manual_start({}))

    def test_auto_restart_from_config(self):
        with patch('env_utils.argv_has', return_value=False):
            with patch('env_utils.env_truthy', return_value=False):
                self.assertFalse(resolve_manual_start({'_auto_restart': True}))

    def test_defaults_to_manual(self):
        with patch('env_utils.argv_has', return_value=False):
            with patch('env_utils.env_truthy', return_value=False):
                self.assertTrue(resolve_manual_start({}))


if __name__ == '__main__':
    unittest.main()
