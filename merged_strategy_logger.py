"""策略维度日志：为价差 / 宽跨输出统一前缀。"""

import logging
from contextlib import contextmanager
from typing import Iterator, Optional

STRATEGY_LABELS = {
    'spread': '价差',
    'strangle': '宽跨',
}


class StrategyLoggerAdapter(logging.LoggerAdapter):
    """在每条日志前加 [价差] / [宽跨]。"""

    def process(self, msg, kwargs):
        label = self.extra.get('label', '')
        if label:
            prefix = f'[{label}] '
            if isinstance(msg, str) and not msg.startswith(prefix):
                msg = prefix + msg
        return msg, kwargs


def strategy_logger(logger: logging.Logger, strategy: str) -> StrategyLoggerAdapter:
    label = STRATEGY_LABELS.get(strategy, strategy)
    return StrategyLoggerAdapter(logger, {'label': label})


def _conn_logger_holders(conn) -> list:
    holders = []
    seen = set()
    for attr in (
        'logger',
        '_instrument_mgr',
        '_reconnect_mgr',
        '_query_svc',
        '_order_mgr',
        'position_tracker',
    ):
        obj = getattr(conn, attr, None)
        if obj is None or id(obj) in seen:
            continue
        if hasattr(obj, 'logger'):
            seen.add(id(obj))
            holders.append(obj)
    return holders


@contextmanager
def strategy_logging(conn, logger, strategy: Optional[str] = None) -> Iterator[logging.LoggerAdapter]:
    """
    策略执行期间使用带前缀 logger，并临时替换 conn 子模块 logger，
    使下单/撤单等走 conn 的日志也带上策略标记。
    """
    tagged = strategy_logger(logger, strategy) if strategy else logger
    snapshots = [(obj, obj.logger) for obj in _conn_logger_holders(conn)]
    for obj, _ in snapshots:
        obj.logger = tagged
    try:
        yield tagged
    finally:
        for obj, prev in snapshots:
            obj.logger = prev
