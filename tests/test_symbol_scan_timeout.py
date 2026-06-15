"""symbol_scan_timeout 单测。"""

import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import symbol_scan_timeout as sst
from symbol_scan_timeout import run_symbol_scan_with_timeout  # noqa: E402


class TestSymbolScanTimeout(unittest.TestCase):

    def setUp(self):
        import symbol_scan_timeout as sst
        sst._active_scans.clear()

    def tearDown(self):
        import symbol_scan_timeout as sst
        sst._active_scans.clear()

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
            time.sleep(0.5)
            done.set()
            return False

        messages = []
        result, timed_out = run_symbol_scan_with_timeout(
            slow, 0.15, 'SA', type('L', (), {'warning': lambda s, m: messages.append(m)})(),
        )
        self.assertIsNone(result)
        self.assertTrue(timed_out)
        self.assertTrue(any('超时' in m for m in messages))

    def test_timed_out_scan_is_tracked_until_background_done(self):
        done = threading.Event()

        def slow():
            time.sleep(0.4)
            done.set()
            return False

        result, timed_out = run_symbol_scan_with_timeout(
            slow, 0.1, 'SA', None,
        )

        self.assertIsNone(result)
        self.assertTrue(timed_out)
        self.assertEqual(sst.wait_for_background_scans(0), 1)
        self.assertTrue(done.wait(1.0))
        self.assertEqual(sst.wait_for_background_scans(1.0), 0)

    def test_propagates_exception_when_not_timed_out(self):
        def boom():
            raise ValueError('boom')

        with self.assertRaises(ValueError):
            run_symbol_scan_with_timeout(boom, 1.0, 'SA', None)

    def test_logs_background_exception_after_timeout(self):
        started = threading.Event()

        def slow_then_fail():
            started.set()
            time.sleep(0.3)
            raise RuntimeError('ctp query failed')

        messages = []
        errors = []

        class _Logger:
            def warning(self, msg):
                messages.append(msg)

            def error(self, msg, exc_info=None):
                errors.append((msg, exc_info))

        result, timed_out = run_symbol_scan_with_timeout(
            slow_then_fail, 0.1, 'SA', _Logger(),
        )
        self.assertIsNone(result)
        self.assertTrue(timed_out)
        self.assertTrue(started.wait(1.0))
        deadline = time.time() + 1.0
        while time.time() < deadline and not errors:
            time.sleep(0.05)
        self.assertTrue(
            any('后台品种扫描异常' in msg for msg, _ in errors),
            f'expected background error log, got {errors}',
        )


if __name__ == '__main__':
    unittest.main()
