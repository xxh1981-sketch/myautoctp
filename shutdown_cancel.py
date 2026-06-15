"""进程退出前撤单：阻断新发单、停执行器/后台扫描、双轮全量撤单。"""

from __future__ import annotations

import threading
import time
from typing import Optional

_SHUTDOWN_FLAG = '_shutdown_cancel'
_SEND_GUARD_INSTALLED = False
_FAST_FAIL_GUARD_INSTALLED = False
_CANCEL_STARTED = False
_CANCEL_LOCK = threading.Lock()


def _safe_float(value, default: float) -> float:
    try:
        if value is None or value == '':
            return float(default)
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _safe_int(value, default: int) -> int:
    try:
        if value is None or value == '':
            return int(default)
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _ensure_shutdown_runtime(conn) -> dict:
    runtime = getattr(conn, '_runtime_state', None)
    if not isinstance(runtime, dict):
        runtime = {}
        setattr(conn, '_runtime_state', runtime)
    return runtime


def is_shutdown_cancel_active(conn) -> bool:
    runtime = getattr(conn, '_runtime_state', None)
    if not isinstance(runtime, dict):
        return False
    return bool(runtime.get(_SHUTDOWN_FLAG))


def _executor_stop_requested(conn) -> bool:
    ex = getattr(conn, '_active_executor', None)
    stop = getattr(ex, 'stop_all_threads', None) if ex is not None else None
    return stop is not None and stop.is_set()


def is_process_shutting_down(conn) -> bool:
    """与 autotrade ``auto_utils.is_process_shutting_down`` 语义一致。"""
    return is_shutdown_cancel_active(conn) or _executor_stop_requested(conn)


def install_shutdown_fast_fail_guards() -> bool:
    """退出置位后 CTP 查询/持仓降级立即失败，避免后台扫描线程 60s 空等。"""
    global _FAST_FAIL_GUARD_INSTALLED
    if _FAST_FAIL_GUARD_INSTALLED:
        return True
    ok = True
    ok = _install_query_wait_guard() and ok
    ok = _install_positions_fallback_guard() and ok
    _FAST_FAIL_GUARD_INSTALLED = ok
    return ok


def _install_query_wait_guard() -> bool:
    try:
        import auto_query_service as aqs
    except ImportError:
        return False

    original = aqs.QueryService._wait_for_td_ready
    if getattr(original, '_shutdown_fast_fail_wrapped', False):
        return True

    def guarded_wait(self, conn, label: str = '查询') -> bool:
        if is_process_shutting_down(conn):
            self.logger.info(f'[{label}] 进程关闭中，跳过查询')
            return False
        return original(self, conn, label)

    guarded_wait._shutdown_fast_fail_wrapped = True  # type: ignore[attr-defined]
    aqs.QueryService._wait_for_td_ready = guarded_wait
    return True


def _install_positions_fallback_guard() -> bool:
    try:
        import auto_utils as au
    except ImportError:
        return False

    original = au.query_positions_fallback
    if getattr(original, '_shutdown_fast_fail_wrapped', False):
        return True

    def guarded_query_positions_fallback(
        conn, timeout=5, logger=None, symbol='', month=None, normalized_month=None,
    ):
        if is_process_shutting_down(conn):
            if logger:
                logger.info(f'[{symbol}] 进程关闭中，跳过持仓查询')
            return None
        result = original(
            conn,
            timeout=timeout,
            logger=logger,
            symbol=symbol,
            month=month,
            normalized_month=normalized_month,
        )
        if result is not None or not is_process_shutting_down(conn):
            return result
        if logger:
            logger.info(f'[{symbol}] 进程关闭中，跳过 tracker 降级')
        return None

    guarded_query_positions_fallback._shutdown_fast_fail_wrapped = True  # type: ignore[attr-defined]
    au.query_positions_fallback = guarded_query_positions_fallback
    return True


def install_shutdown_send_guard() -> bool:
    """发单守卫：``_shutdown_cancel`` 置位后拒绝一切新开/平仓发单。"""
    global _SEND_GUARD_INSTALLED
    if _SEND_GUARD_INSTALLED:
        return True
    try:
        import auto_order_manager as aom
    except ImportError as e:
        return False

    original = aom.OrderManager.send_order
    if getattr(original, '_shutdown_cancel_wrapped', False):
        _SEND_GUARD_INSTALLED = True
        return True

    def guarded_send_order(self, instrument, *args, **kwargs):
        conn = self.conn
        if is_shutdown_cancel_active(conn):
            self.logger.info(
                f'[退出] 进程关闭中，拒绝发单: {instrument}',
            )
            return None, None
        return original(self, instrument, *args, **kwargs)

    guarded_send_order._shutdown_cancel_wrapped = True  # type: ignore[attr-defined]
    aom.OrderManager.send_order = guarded_send_order
    _SEND_GUARD_INSTALLED = True
    return True


def _cancel_force_exit_timer() -> None:
    try:
        import merged_main

        merged_main._cancel_shutdown_timer()
    except Exception:
        pass


def _stop_active_executor(conn, logger) -> None:
    lock = getattr(conn, '_executor_lock', None)
    if lock is None:
        ex = getattr(conn, '_active_executor', None)
        if ex:
            try:
                ex.stop_all_threads.set()
                ex.cleanup()
            except Exception:
                pass
            conn._active_executor = None
        return
    with lock:
        ex = getattr(conn, '_active_executor', None)
        if not ex:
            return
        try:
            ex.stop_all_threads.set()
            ex.cleanup()
        except Exception as e:
            if logger:
                logger.debug(f'[退出] 停价差执行器异常: {e}')
        conn._active_executor = None
        if logger:
            logger.info('[退出] 已停价差执行器')


def _ensure_td_for_shutdown(conn, config: dict, logger) -> bool:
    if getattr(conn, 'td_logined', False) and getattr(conn, 'td_api', None):
        return True
    if logger:
        logger.info('[退出] 交易未连接，尝试短连后撤单…')
    login_timeout = _safe_float(
        config.get('shutdown_cancel_login_timeout', 30), 30,
    )
    try:
        mgr = getattr(conn, '_reconnect_mgr', None)
        if mgr is not None:
            mgr.cancel_reconnect_timer()
        if not getattr(conn, 'td_api', None):
            conn.connect_trading()
        start = time.time()
        while time.time() - start < login_timeout:
            if getattr(conn, 'td_logined', False) and getattr(conn, 'td_api', None):
                if logger:
                    logger.info('[退出] 交易短连成功，继续撤单')
                return True
            time.sleep(0.5)
        if logger:
            logger.warning(
                f'[退出] 交易短连超时 ({login_timeout:.0f}s)，无法向交易所撤单',
            )
    except Exception as e:
        if logger:
            logger.warning(f'[退出] 交易短连异常: {e}')
    return False


def prepare_shutdown(conn, config: Optional[dict], logger) -> None:
    """主循环 finally 首步：阻断新发单并尽量停掉在途策略线程。"""
    if conn is None:
        return
    cfg = config if isinstance(config, dict) else getattr(conn, 'config', None) or {}
    _ensure_shutdown_runtime(conn)[_SHUTDOWN_FLAG] = True
    _stop_active_executor(conn, logger)
    drain_sec = _safe_float(cfg.get('shutdown_scan_drain_sec', 5), 5)
    if drain_sec > 0:
        try:
            from symbol_scan_timeout import wait_for_background_scans

            remaining = wait_for_background_scans(drain_sec)
            if remaining and logger:
                logger.warning(
                    f'[退出] {remaining} 个品种扫描后台线程仍在运行'
                    f'（已等待 {drain_sec:.0f}s，将继续双轮撤单）',
                )
        except Exception as e:
            if logger:
                logger.debug(f'[退出] 等待后台扫描异常: {e}')


def cancel_pending_on_shutdown(conn, config: Optional[dict], logger) -> int:
    """Best-effort 全量撤单（双轮）；失败仅打日志，不阻断 release/退出。"""
    global _CANCEL_STARTED
    if conn is None:
        return 0
    with _CANCEL_LOCK:
        if _CANCEL_STARTED:
            if logger:
                logger.debug('[退出] 撤单流程已在运行，跳过重复撤单')
            return 0
    cfg = config if isinstance(config, dict) else getattr(conn, 'config', None) or {}
    prepare_shutdown(conn, cfg, logger)
    _cancel_force_exit_timer()
    if not _ensure_td_for_shutdown(conn, cfg, logger):
        return 0

    with _CANCEL_LOCK:
        if _CANCEL_STARTED:
            if logger:
                logger.debug('[退出] 撤单流程已在运行，跳过重复撤单')
            return 0
        _CANCEL_STARTED = True

    passes = max(1, _safe_int(cfg.get('shutdown_cancel_passes', 2), 2))
    pause_sec = _safe_float(cfg.get('shutdown_cancel_pass_pause_sec', 1.0), 1.0)
    query_timeout = _safe_float(cfg.get('CANCEL_ALL_TIMEOUT', 5), 5)
    confirm_timeout = _safe_float(cfg.get('shutdown_cancel_confirm_timeout', 15), 15)
    old_confirm = cfg.get('CANCEL_CONFIRM_TIMEOUT')
    cfg['CANCEL_CONFIRM_TIMEOUT'] = confirm_timeout

    total = 0
    try:
        for i in range(passes):
            try:
                count = conn.cancel_all_pending_orders(timeout=query_timeout)
                total += int(count or 0)
                if logger and int(count or 0) > 0:
                    logger.info(
                        f'[退出] 第 {i + 1}/{passes} 轮撤单: {int(count)} 笔',
                    )
            except Exception as e:
                if logger:
                    logger.error(f'[退出] 第 {i + 1} 轮撤单失败: {e}')
            if i + 1 < passes and pause_sec > 0:
                time.sleep(pause_sec)
    finally:
        if old_confirm is None:
            cfg.pop('CANCEL_CONFIRM_TIMEOUT', None)
        else:
            cfg['CANCEL_CONFIRM_TIMEOUT'] = old_confirm

    if logger and total > 0:
        logger.info(f'[退出] 撤单合计 {total} 笔')
    return total
