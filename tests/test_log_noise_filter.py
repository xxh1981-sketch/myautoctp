"""log_noise_filter 单元测试。"""

import logging
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from log_noise_filter import (  # noqa: E402
    LogNoiseFilter,
    build_filter_from_config,
    install_log_noise_filter,
)


class _CaptureHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        # 记录 handler 实际收到的（级别名 + 文本）
        self.records.append((record.levelname, record.getMessage()))

    @property
    def messages(self):
        return [m for _, m in self.records]


def _make_logger(name, flt):
    logger = logging.getLogger(name)
    logger.handlers.clear()
    logger.filters.clear()
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    cap = _CaptureHandler()
    cap.setLevel(logging.DEBUG)
    logger.addHandler(cap)
    if flt is not None:
        logger.addFilter(flt)
    return logger, cap


class TestThrottle(unittest.TestCase):

    def test_identical_message_throttled_within_window(self):
        flt = LogNoiseFilter(['提升次近月为近月'], window_sec=60, downgrade_rules=())
        logger, cap = _make_logger('test.noise.throttle', flt)

        msg = '[宽跨] [rm] VIX: 近月RM607 OI(0) < 次近月RM608 OI(9238)/2，提升次近月为近月'
        for _ in range(11):
            logger.info(msg)

        # 同一秒内 11 次重复 → 只过 1 次
        self.assertEqual(cap.messages.count(msg), 1)

    def test_different_symbols_not_collapsed(self):
        flt = LogNoiseFilter(['提升次近月为近月'], window_sec=60, downgrade_rules=())
        logger, cap = _make_logger('test.noise.distinct', flt)

        rm = '[宽跨] [rm] VIX: 提升次近月为近月'
        m = '[宽跨] [m] VIX: 提升次近月为近月'
        for _ in range(5):
            logger.info(rm)
            logger.info(m)

        self.assertEqual(cap.messages.count(rm), 1)
        self.assertEqual(cap.messages.count(m), 1)

    def test_non_matching_message_never_throttled(self):
        flt = LogNoiseFilter(['提升次近月为近月'], window_sec=60, downgrade_rules=())
        logger, cap = _make_logger('test.noise.passthrough', flt)

        trade_msg = '[价差] [ag] VIX=47.82 > 阈值2.00,触发交易!'
        for _ in range(7):
            logger.info(trade_msg)

        self.assertEqual(cap.messages.count(trade_msg), 7)

    def test_window_expiry_allows_again(self):
        flt = LogNoiseFilter(['提升次近月为近月'], window_sec=60, downgrade_rules=())
        logger, cap = _make_logger('test.noise.window', flt)
        msg = '[宽跨] [m] VIX: 提升次近月为近月'

        logger.info(msg)
        # 模拟窗口已过：把记录时间戳往前拨
        for key in list(flt._seen):
            flt._seen[key] -= 120
        logger.info(msg)

        self.assertEqual(cap.messages.count(msg), 2)

    def test_window_zero_disables_throttle(self):
        flt = LogNoiseFilter(['提升次近月为近月'], window_sec=0, downgrade_rules=())
        logger, cap = _make_logger('test.noise.window0', flt)
        msg = '[宽跨] [m] VIX: 提升次近月为近月'
        for _ in range(4):
            logger.info(msg)
        self.assertEqual(cap.messages.count(msg), 4)


class TestDowngrade(unittest.TestCase):

    def test_error_downgraded_to_warning(self):
        flt = LogNoiseFilter(
            throttle_substrings=(),
            window_sec=0,
            downgrade_rules=[('当前状态禁止此项操作', logging.WARNING)],
        )
        logger, cap = _make_logger('test.noise.downgrade', flt)

        logger.error('[撤单] 错误回报: 26:DCE:当前状态禁止此项操作')

        self.assertEqual(len(cap.records), 1)
        level, _ = cap.records[0]
        self.assertEqual(level, 'WARNING')

    def test_unmatched_error_kept_as_error(self):
        flt = LogNoiseFilter(
            throttle_substrings=('提升次近月为近月',),
            window_sec=60,
            downgrade_rules=[('当前状态禁止此项操作', logging.WARNING)],
        )
        logger, cap = _make_logger('test.noise.keep_error', flt)

        logger.error('[撤单] 错误回报: 真实故障保留')

        self.assertEqual(cap.records[0][0], 'ERROR')
        self.assertIn('真实故障保留', cap.records[0][1])

    def test_real_error_in_throttle_pattern_not_dropped(self):
        # 即便错误文本恰好命中节流子串，ERROR 也绝不被丢弃。
        flt = LogNoiseFilter(
            throttle_substrings=('VIX无法计算',),
            window_sec=60,
            downgrade_rules=(),
        )
        logger, cap = _make_logger('test.noise.error_not_dropped', flt)

        for _ in range(3):
            logger.error('严重: VIX无法计算 且引擎崩溃')

        self.assertEqual(len(cap.records), 3)

    def test_downgraded_then_throttled(self):
        # 降级为 WARNING 后，命中节流子串则窗口内只过一次。
        flt = LogNoiseFilter(
            throttle_substrings=('当前状态禁止此项操作',),
            window_sec=60,
            downgrade_rules=[('当前状态禁止此项操作', logging.WARNING)],
        )
        logger, cap = _make_logger('test.noise.downgrade_throttle', flt)

        for _ in range(16):
            logger.error('[撤单] 错误回报: 26:DCE:当前状态禁止此项操作')

        self.assertEqual(len(cap.records), 1)
        self.assertEqual(cap.records[0][0], 'WARNING')


class TestBuildAndInstall(unittest.TestCase):

    def test_build_disabled_returns_none(self):
        self.assertIsNone(build_filter_from_config({'log_noise': {'enabled': False}}))

    def test_build_defaults_when_absent(self):
        flt = build_filter_from_config({})
        self.assertIsInstance(flt, LogNoiseFilter)
        self.assertIn('提升次近月为近月', flt._subs)

    def test_build_string_substring_coerced_to_list(self):
        flt = build_filter_from_config(
            {'log_noise': {'throttle_substrings': '仅一个子串'}}
        )
        self.assertEqual(flt._subs, ('仅一个子串',))

    def test_install_idempotent(self):
        logger = logging.getLogger('test.noise.install')
        logger.filters.clear()
        ok1 = install_log_noise_filter(logger, {})
        ok2 = install_log_noise_filter(logger, {})
        self.assertTrue(ok1 and ok2)
        marked = [
            f for f in logger.filters
            if getattr(f, '_autoctp_log_noise_filter', False)
        ]
        self.assertEqual(len(marked), 1)

    def test_install_disabled_removes_existing(self):
        logger = logging.getLogger('test.noise.install_off')
        logger.filters.clear()
        install_log_noise_filter(logger, {})
        install_log_noise_filter(logger, {'log_noise': {'enabled': False}})
        marked = [
            f for f in logger.filters
            if getattr(f, '_autoctp_log_noise_filter', False)
        ]
        self.assertEqual(len(marked), 0)


if __name__ == '__main__':
    unittest.main()
