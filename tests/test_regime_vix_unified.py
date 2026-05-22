"""Regime VIX: strangle uses same calculate_vix path as spread."""

import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from straggle_vix import (
        MERGED_REGIME_VIX_CACHE_KEY,
        calculate_vix_for_month,
    )
    _HAS_STRAGGLE = True
except ImportError:
    _HAS_STRAGGLE = False


@unittest.skipUnless(_HAS_STRAGGLE, 'autostraggle not on path')
class TestRegimeVixUnified(unittest.TestCase):
    def setUp(self):
        import straggle_vix as mod
        mod._VIX_MONTH_ALIGN_LOGGED.clear()

    def test_delegates_to_engine_calculate_vix(self):
        engine = MagicMock()
        engine.calculate_vix.return_value = 25.5
        conn = MagicMock()
        conn._runtime_state = {}
        conn._normalize_month = lambda s, m: str(m)

        vix = calculate_vix_for_month(engine, 'ma', '609', conn, None)
        self.assertEqual(vix, 25.5)
        engine.calculate_vix.assert_called_once()

    def test_uses_merged_round_cache(self):
        engine = MagicMock()
        engine.calculate_vix.return_value = 30.0
        conn = MagicMock()
        conn._runtime_state = {MERGED_REGIME_VIX_CACHE_KEY: {}}
        conn._normalize_month = lambda s, m: str(m)

        v1 = calculate_vix_for_month(engine, 'ma', '609', conn, None)
        v2 = calculate_vix_for_month(engine, 'ma', '609', conn, None)
        self.assertEqual(v1, 30.0)
        self.assertEqual(v2, 30.0)
        engine.calculate_vix.assert_called_once()

    def test_logs_month_mismatch_once(self):
        engine = MagicMock()
        engine.calculate_vix.return_value = 20.0
        engine.discover_nearby_next.return_value = {'nearby_month': '608'}
        conn = MagicMock()
        conn._runtime_state = {}
        conn._normalize_month = lambda s, m: str(m)
        logger = MagicMock()

        calculate_vix_for_month(engine, 'ma', '609', conn, logger)
        calculate_vix_for_month(engine, 'ma', '609', conn, logger)
        self.assertEqual(logger.info.call_count, 1)
        msg = logger.info.call_args[0][0]
        self.assertIn('608', msg)
        self.assertIn('609', msg)


if __name__ == '__main__':
    unittest.main()
