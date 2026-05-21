"""merged_strategy_logger 单元测试。"""

import logging
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from merged_strategy_logger import strategy_logger, strategy_logging


class _CaptureHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


class TestMergedStrategyLogger(unittest.TestCase):

    def test_strategy_logger_prefix(self):
        base = logging.getLogger('test.strategy.prefix')
        base.handlers.clear()
        cap = _CaptureHandler()
        base.addHandler(cap)
        base.setLevel(logging.INFO)

        slog = strategy_logger(base, 'spread')
        slog.info('开仓检查')

        self.assertEqual(cap.messages[-1], '[价差] 开仓检查')

    def test_strategy_logging_restores_conn_loggers(self):
        conn = type('Conn', (), {})()
        holder = type('Holder', (), {})()
        holder.logger = logging.getLogger('holder.orig')
        conn._order_mgr = holder
        original = holder.logger
        base = logging.getLogger('test.strategy.restore')

        with strategy_logging(conn, base, 'strangle'):
            tagged = holder.logger
            self.assertIsNot(tagged, original)

        self.assertIs(holder.logger, original)


class TestStrategyLoggingContext(unittest.TestCase):

    def test_temporarily_tags_conn_submodule_loggers(self):
        conn = type('Conn', (), {})()
        holder = type('Holder', (), {})()
        holder.logger = logging.getLogger('holder.capture')
        conn._order_mgr = holder
        base = logging.getLogger('test.strategy.capture')
        cap = _CaptureHandler()
        base.addHandler(cap)
        base.setLevel(logging.INFO)

        with strategy_logging(conn, base, 'spread'):
            holder.logger.info('发单')

        self.assertTrue(any('[价差]' in msg for msg in cap.messages))


if __name__ == '__main__':
    unittest.main()
