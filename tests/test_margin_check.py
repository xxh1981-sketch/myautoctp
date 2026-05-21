"""margin_check integration tests.

与 ``test_margin_check_unit.py`` 重复的三态逻辑已迁至 unit 套件。
本文件保留为空壳，便于本地有 autotrade 时仍收集 integration 标记；
全量行为由 ``test_margin_check_unit.py`` 覆盖。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ctp_bootstrap  # noqa: F401

import margin_check


class TestMarginCheckIntegrationSmoke(unittest.TestCase):
    """确认 autotrade 环境下 margin_check 可 import 且接口存在。"""

    def test_module_exports(self):
        self.assertTrue(callable(margin_check.check_margin_status))
        self.assertTrue(callable(margin_check.check_margin))


if __name__ == '__main__':
    unittest.main()
