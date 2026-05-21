"""spread_derive integration smoke.

核心推导逻辑见 ``test_spread_derive_unit.py``（CI unit 必跑）。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ctp_bootstrap  # noqa: F401

from spread_derive import derive_spread_claims_from_ctp


class TestSpreadDeriveIntegrationSmoke(unittest.TestCase):

    def test_module_importable_with_autotrade(self):
        self.assertTrue(callable(derive_spread_claims_from_ctp))


if __name__ == '__main__':
    unittest.main()
