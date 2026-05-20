"""spread_ledger_execution unit tests"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ctp_bootstrap  # noqa: F401

from spread_dual_config import spread_execution_from_ledger


class TestSpreadLedgerExecution(unittest.TestCase):

    def test_spread_execution_flag_master(self):
        cfg = {
            'dual_strategy': {
                'use_spread_leg_claims': True,
                'spread_execution_from_ledger': True,
            },
        }
        self.assertTrue(spread_execution_from_ledger(cfg))

    def test_spread_execution_flag_legacy_close_alias(self):
        cfg = {
            'dual_strategy': {
                'use_spread_leg_claims': True,
                'spread_execution_from_ledger': False,
                'spread_close_from_ledger': True,
            },
        }
        self.assertTrue(spread_execution_from_ledger(cfg))

    def test_spread_execution_disabled_without_claims(self):
        cfg = {'dual_strategy': {'use_spread_leg_claims': False}}
        self.assertFalse(spread_execution_from_ledger(cfg))

    def test_module_imports(self):
        import spread_ledger_execution as sle
        self.assertTrue(callable(sle.install_spread_ledger_execution))
        self.assertTrue(callable(sle._rebind_analyze_consumers))


if __name__ == '__main__':
    unittest.main()
