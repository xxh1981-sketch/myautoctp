"""weekend_pause_patch 双休日非交易抑制单测。"""

import os
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import weekend_pause_patch as wpp  # noqa: E402


class _FakeLogger:
    def __init__(self):
        self.messages = []

    def info(self, msg, *a, **k):
        self.messages.append(msg)


class TestIsWeekendNontrading(unittest.TestCase):
    def test_sunday_all_day(self):
        cfg = {}
        # 2026-05-31 是周日
        self.assertTrue(wpp.is_weekend_nontrading(cfg, datetime(2026, 5, 31, 0, 1)))
        self.assertTrue(wpp.is_weekend_nontrading(cfg, datetime(2026, 5, 31, 21, 0)))
        self.assertTrue(wpp.is_weekend_nontrading(cfg, datetime(2026, 5, 31, 23, 59)))

    def test_saturday_only_after_cutoff(self):
        cfg = {}
        # 2026-05-30 是周六；默认 cutoff=5
        self.assertFalse(wpp.is_weekend_nontrading(cfg, datetime(2026, 5, 30, 2, 0)))
        self.assertFalse(wpp.is_weekend_nontrading(cfg, datetime(2026, 5, 30, 4, 59)))
        self.assertTrue(wpp.is_weekend_nontrading(cfg, datetime(2026, 5, 30, 5, 0)))
        self.assertTrue(wpp.is_weekend_nontrading(cfg, datetime(2026, 5, 30, 9, 0)))

    def test_saturday_cutoff_configurable(self):
        cfg = {'weekend_pause_saturday_from_hour': 8}
        self.assertFalse(wpp.is_weekend_nontrading(cfg, datetime(2026, 5, 30, 7, 0)))
        self.assertTrue(wpp.is_weekend_nontrading(cfg, datetime(2026, 5, 30, 8, 0)))

    def test_weekdays_never_weekend(self):
        cfg = {}
        # 周一..周五（2026-05-25 ~ 05-29），含周五夜盘 21:00
        for day in range(25, 30):
            for hour in (0, 9, 13, 21, 23):
                self.assertFalse(
                    wpp.is_weekend_nontrading(cfg, datetime(2026, 5, day, hour)),
                    f'2026-05-{day} {hour}:00 不应判为周末',
                )

    def test_disabled_flag(self):
        cfg = {'weekend_pause_enabled': False}
        self.assertFalse(wpp.is_weekend_nontrading(cfg, datetime(2026, 5, 31, 12, 0)))


class TestPatchedWrappers(unittest.TestCase):
    def setUp(self):
        self._orig_is = wpp._orig_is_suspended
        self._orig_rec = wpp._orig_check_recovery

    def tearDown(self):
        wpp._orig_is_suspended = self._orig_is
        wpp._orig_check_recovery = self._orig_rec

    def test_is_suspended_weekend_short_circuits(self):
        called = {'n': 0}

        def fake_orig(config, now=None):
            called['n'] += 1
            return False

        wpp._orig_is_suspended = fake_orig
        # 周日 → 直接 True，不调原函数
        self.assertTrue(
            wpp.patched_is_connection_suspended({}, datetime(2026, 5, 31, 12, 0)),
        )
        self.assertEqual(called['n'], 0)

    def test_is_suspended_weekday_delegates(self):
        def fake_orig(config, now=None):
            return 'orig-result'

        wpp._orig_is_suspended = fake_orig
        # 周三 → 委托原函数
        self.assertEqual(
            wpp.patched_is_connection_suspended({}, datetime(2026, 5, 27, 12, 0)),
            'orig-result',
        )

    def test_check_recovery_skips_on_weekend(self):
        class Conn:
            _runtime_state = {}

        called = {'n': 0}

        def fake_rec(conn, config, logger):
            called['n'] += 1
            return True

        wpp._orig_check_recovery = fake_rec
        conn = Conn()
        logger = _FakeLogger()
        import unittest.mock as mock
        with mock.patch.object(wpp, 'is_weekend_nontrading', return_value=True):
            out = wpp.patched_check_scheduled_full_recovery(conn, {}, logger)
        self.assertFalse(out)
        self.assertEqual(called['n'], 0)
        self.assertTrue(conn._runtime_state.get('_weekend_pause_logged'))
        self.assertEqual(len(logger.messages), 1)
        # 二次调用不重复日志
        with mock.patch.object(wpp, 'is_weekend_nontrading', return_value=True):
            wpp.patched_check_scheduled_full_recovery(conn, {}, logger)
        self.assertEqual(len(logger.messages), 1)

    def test_check_recovery_delegates_on_weekday(self):
        class Conn:
            _runtime_state = {'_weekend_pause_logged': True}

        def fake_rec(conn, config, logger):
            return 'delegated'

        wpp._orig_check_recovery = fake_rec
        conn = Conn()
        import unittest.mock as mock
        with mock.patch.object(wpp, 'is_weekend_nontrading', return_value=False):
            out = wpp.patched_check_scheduled_full_recovery(conn, {}, _FakeLogger())
        self.assertEqual(out, 'delegated')
        # 工作日清除周末标志
        self.assertNotIn('_weekend_pause_logged', conn._runtime_state)


if __name__ == '__main__':
    unittest.main()
