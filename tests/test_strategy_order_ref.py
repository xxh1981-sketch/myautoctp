"""双策略 OrderRef 分段单元测试"""

import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ctp_bootstrap  # noqa: F401

from auto_strategy_order_ref import (
    allocate_order_ref,
    get_spread_order_ref_max,
    get_strangle_order_ref_min,
    init_order_ref_sequences,
    is_spread_order_ref,
    is_strangle_order_ref,
    STRATEGY_SPREAD,
    STRATEGY_STRANGLE,
)


def _cfg():
    return {
        'strangle': {'order_ref_min': 500000},
        'dual_strategy': {'spread_order_ref_max': 499999},
    }


class TestOrderRefSegments(unittest.TestCase):

    def test_thresholds(self):
        cfg = _cfg()
        self.assertEqual(get_strangle_order_ref_min(cfg), 500000)
        self.assertEqual(get_spread_order_ref_max(cfg), 499999)

    def test_is_strangle_vs_spread(self):
        cfg = _cfg()
        self.assertTrue(is_strangle_order_ref(500000, cfg))
        self.assertTrue(is_strangle_order_ref(500001, cfg))
        self.assertFalse(is_strangle_order_ref(499999, cfg))
        self.assertTrue(is_spread_order_ref(1, cfg))
        self.assertTrue(is_spread_order_ref(499999, cfg))
        self.assertFalse(is_spread_order_ref(500000, cfg))

    def test_allocate_separate_sequences(self):
        cfg = _cfg()
        conn = MagicMock()
        conn._request_id_lock = __import__('threading').Lock()
        conn._spread_order_ref_seq = 0
        conn._strangle_order_ref_seq = 0

        init_order_ref_sequences(conn, cfg)
        spread_refs = [allocate_order_ref(conn, STRATEGY_SPREAD, cfg) for _ in range(3)]
        str_refs = [allocate_order_ref(conn, STRATEGY_STRANGLE, cfg) for _ in range(3)]

        self.assertEqual(spread_refs, [1, 2, 3])
        self.assertEqual(str_refs, [500000, 500001, 500002])
        for r in spread_refs:
            self.assertTrue(is_spread_order_ref(r, cfg))
        for r in str_refs:
            self.assertTrue(is_strangle_order_ref(r, cfg))


if __name__ == '__main__':
    unittest.main()
