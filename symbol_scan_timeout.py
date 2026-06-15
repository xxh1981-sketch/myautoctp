"""单品种扫描超时：防止 CTP 查询/策略处理挂死阻塞整轮主循环。

在独立线程中执行 ``process_*_symbol``；超时后主循环继续（心跳/报平安不受影响）。
超时后工作线程可能仍在后台运行（CTP 调用无法强杀），属有意权衡。
``max_symbol_scan_sec <= 0`` 时禁用，与 ``max_strangle_scan_sec`` 整轮预算互补。

退出时 ``wait_for_background_scans`` 尽量等待后台线程结束（无法强杀时配合
``shutdown_cancel`` 的发单守卫 + 双轮撤单）。
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from typing import Callable, Optional, Set, Tuple, TypeVar

T = TypeVar('T')

_active_scans_lock = threading.Lock()
_active_scans: Set[Future] = set()


def _track_future(future: Future) -> None:
    with _active_scans_lock:
        _active_scans.add(future)


def _untrack_future(future: Future) -> None:
    with _active_scans_lock:
        _active_scans.discard(future)


def wait_for_background_scans(timeout_sec: float) -> int:
    """等待在途品种扫描线程；返回超时后仍在运行的数量。"""
    if timeout_sec <= 0:
        with _active_scans_lock:
            return sum(1 for f in _active_scans if not f.done())
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        with _active_scans_lock:
            pending = [f for f in _active_scans if not f.done()]
        if not pending:
            return 0
        time.sleep(0.2)
    with _active_scans_lock:
        return sum(1 for f in _active_scans if not f.done())


def _scan_done_callback(symbol: str, logger) -> Callable[[Future], None]:
    """Future 完成时 untrack；若后台线程抛错则补日志（超时后主循环已继续）。"""

    def _done(_f: Future) -> None:
        _untrack_future(_f)
        if logger is None:
            return
        exc = _f.exception()
        if exc is None:
            return
        logger.error(
            f'[{symbol}] 后台品种扫描异常: {exc}',
            exc_info=exc,
        )

    return _done


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
    future = executor.submit(fn)
    _track_future(future)
    try:
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
        future.add_done_callback(_scan_done_callback(symbol, logger))
        executor.shutdown(wait=False, cancel_futures=True)
