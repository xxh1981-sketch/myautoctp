"""ctp_recovery_patch 辅助逻辑单元测试。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ctp_recovery_patch as crp


class TestRecoveryPatchHelpers(unittest.TestCase):

    def test_pending_status_set(self):
        self.assertTrue(crp._is_pending_status('1'))
        self.assertTrue(crp._is_pending_status('3'))
        self.assertTrue(crp._is_pending_status('a'))
        self.assertFalse(crp._is_pending_status('0'))
        self.assertFalse(crp._is_pending_status('5'))

    def test_install_idempotent(self):
        crp._INSTALLED = False
        crp.install_recovery_patch()
        state_after_first = crp._INSTALLED
        crp.install_recovery_patch()
        self.assertEqual(crp._INSTALLED, state_after_first)


if __name__ == '__main__':
    unittest.main()
