"""单品种扫描超时：防止 CTP 查询/策略处理挂死阻塞整轮主循环。

在独立线程中执行 ``process_*_symbol``；超时后主循环继续（心跳/报平安不受影响）。
超时后工作线程可能仍在后台运行（CTP 调用无法强杀），属有意权衡。
``max_symbol_scan_sec <= 0`` 时禁用，与 ``max_strangle_scan_sec`` 整轮预算互补。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from typing import Callable, Optional, Tuple, TypeVar

T = TypeVar('T')


def run_symbol_scan_with_timeout(
    fn: Callable[[], T],
    timeout_sec: float,
    symbol: str,
    logger=None,
) -> Tuple[Optional[T], bool]:
    """执行 fn；返回 ``(result, timed_out)``。超时时 result 为 None。"""
    if timeout_sec <= 0:
        return fn(), False

    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='symbol_scan')
    try:
        future = executor.submit(fn)
        try:
            return future.result(timeout=timeout_sec), False
        except FuturesTimeoutError:
            if logger:
                logger.warning(
                    f'[{symbol}] 品种扫描超时 ({timeout_sec:.0f}s)，'
                    '跳过本品种（后台线程可能仍在运行，主循环继续）'
                )
            return None, True
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
