"""symbol_scan_timeout 单测。"""

import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from symbol_scan_timeout import run_symbol_scan_with_timeout  # noqa: E402


class TestSymbolScanTimeout(unittest.TestCase):
    def test_disabled_when_zero(self):
        result, timed_out = run_symbol_scan_with_timeout(
            lambda: 42, 0, 'SA', None,
        )
        self.assertEqual(result, 42)
        self.assertFalse(timed_out)

    def test_returns_result_when_fast(self):
        result, timed_out = run_symbol_scan_with_timeout(
            lambda: True, 2.0, 'SA', None,
        )
        self.assertTrue(result)
        self.assertFalse(timed_out)

    def test_times_out_on_slow_call(self):
        done = threading.Event()

        def slow():
            time.sleep(5)
            done.set()
            return False

        messages = []
        result, timed_out = run_symbol_scan_with_timeout(
            slow, 0.15, 'SA', type('L', (), {'warning': lambda s, m: messages.append(m)})(),
        )
        self.assertIsNone(result)
        self.assertTrue(timed_out)
        self.assertTrue(any('超时' in m for m in messages))

    def test_propagates_exception_when_not_timed_out(self):
        def boom():
            raise ValueError('boom')

        with self.assertRaises(ValueError):
            run_symbol_scan_with_timeout(boom, 1.0, 'SA', None)


if __name__ == '__main__':
    unittest.main()
