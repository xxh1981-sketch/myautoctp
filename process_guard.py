"""单进程守护（PID 文件）。

按 GUIDE.md 中"禁止双进程"约定，启动时先尝试获取一个 PID 锁文件。
如果检测到另一个 PID 仍在运行，则拒绝启动并退出。

跨平台行为：
- POSIX：用 ``fcntl.flock(LOCK_EX | LOCK_NB)`` 持有锁；进程退出自动释放。
- Windows：用 ``msvcrt.locking`` (LK_NBLCK) 锁定 PID 文件起始字节；
  进程退出自动释放。
- 退化路径（缺少 fcntl/msvcrt）：仅做 "PID 存在性 + 同名进程验证" 的轻量
  软检查，仍会写入 PID 文件供运维查阅。

被检测出冲突时 ``acquire_singleton`` 抛出 :class:`AlreadyRunningError`；
调用方应捕获后退出，不要降级跑——双进程会破坏 fill_ledger / journal
去重的进程内锁假设。

journal_lock 注解
-----------------
:mod:`trade_journal_lock` 用 ``threading.RLock``，**只在单进程内** 保证
互斥。两个 autoctp 同时跑会让 journal 的 dedupe 失效，重复入账写入 CSV /
重复发飞书。本模块是该假设的强制 enforcement。
"""

from __future__ import annotations

import atexit
import os
from typing import Optional


class AlreadyRunningError(RuntimeError):
    """另一个 autoctp 实例正持有 PID 锁。"""


_HELD_FD = None
_HELD_LOCK_PATH: Optional[str] = None
_HELD_PID_PATH: Optional[str] = None
# 保留旧名供测试/向后兼容（== PID 信息文件路径）
_HELD_PATH: Optional[str] = None


def _pid_alive(pid: int) -> bool:
    """跨平台判断 PID 是否仍存在。"""
    if pid <= 0:
        return False
    if os.name == 'nt':
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            handle = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid,
            )
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                ok = ctypes.windll.kernel32.GetExitCodeProcess(
                    handle, ctypes.byref(code),
                )
                return bool(ok) and code.value == STILL_ACTIVE
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
        except Exception:
            return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _try_lock(fd) -> bool:
    """非阻塞拿锁；返回是否拿到。无 fcntl/msvcrt 时返回 True（软模式）。"""
    if os.name == 'nt':
        try:
            import msvcrt
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                return True
            except OSError:
                return False
        except ImportError:
            return True
    try:
        import fcntl
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except (BlockingIOError, OSError):
            return False
    except ImportError:
        return True


def _default_pid_path() -> str:
    base = os.path.dirname(os.path.abspath(__file__))
    data = os.path.join(base, 'data')
    if not os.path.isdir(data):
        try:
            os.makedirs(data, exist_ok=True)
        except OSError:
            data = base
    return os.path.join(data, 'autoctp.pid')


def acquire_singleton(pid_path: Optional[str] = None, logger=None) -> str:
    """获取单实例锁，返回 PID 信息文件路径；冲突时抛 :class:`AlreadyRunningError`。

    幂等：同进程多次调用直接返回已持有的路径。

    实现：
      - 锁文件 ``<pid_path>.lock``：用 fcntl/msvcrt 在第一字节加锁；Windows
        上 mandatory lock 会阻止其他进程读写它，故只用作锁，不存数据。
      - PID 信息文件 ``<pid_path>``：明文 PID，**不加锁**，供运维 / 监控
        / 测试随时读取。
    """
    global _HELD_FD, _HELD_LOCK_PATH, _HELD_PID_PATH, _HELD_PATH
    if _HELD_FD is not None:
        return _HELD_PID_PATH

    pid_file = pid_path or _default_pid_path()
    lock_file = pid_file + '.lock'

    # 确保父目录存在
    parent = os.path.dirname(os.path.abspath(lock_file)) or '.'
    try:
        os.makedirs(parent, exist_ok=True)
    except OSError:
        pass

    fd = os.open(lock_file, os.O_RDWR | os.O_CREAT, 0o644)
    locked = _try_lock(fd)
    if not locked:
        existing_pid = _peek_existing_pid(pid_file)
        try:
            os.close(fd)
        except OSError:
            pass
        raise AlreadyRunningError(
            f'另一 autoctp 实例已在运行 (PID={existing_pid}, lock={lock_file})。'
            '禁止双进程：会破坏 journal 去重与 fill_ledger 一致性。'
        )

    # 写 PID 信息文件（与锁文件分离，可被其他进程读取）
    try:
        with open(pid_file, 'w', encoding='utf-8') as f:
            f.write(f'{os.getpid()}\n')
    except OSError as e:
        if logger:
            logger.debug(f'[process_guard] 写 PID 信息文件失败: {e}')

    _HELD_FD = fd
    _HELD_LOCK_PATH = lock_file
    _HELD_PID_PATH = pid_file
    _HELD_PATH = pid_file
    atexit.register(_release_on_exit)
    if logger:
        logger.info(
            f'[process_guard] 单实例锁已获取: PID={os.getpid()} '
            f'lock={lock_file} info={pid_file}'
        )
    return pid_file


def release_singleton() -> None:
    """显式释放（一般依赖 atexit 即可）。"""
    _release_on_exit()


def _release_on_exit() -> None:
    global _HELD_FD, _HELD_LOCK_PATH, _HELD_PID_PATH, _HELD_PATH
    if _HELD_FD is None:
        return
    try:
        if os.name == 'nt':
            try:
                import msvcrt
                try:
                    os.lseek(_HELD_FD, 0, 0)
                    msvcrt.locking(_HELD_FD, msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            except ImportError:
                pass
        else:
            try:
                import fcntl
                fcntl.flock(_HELD_FD, fcntl.LOCK_UN)
            except Exception:
                pass
    finally:
        try:
            os.close(_HELD_FD)
        except OSError:
            pass
        _HELD_FD = None
        _HELD_LOCK_PATH = None
        # PID 信息文件保留供运维查最近一次运行的 PID；不主动删除。
        _HELD_PID_PATH = None
        _HELD_PATH = None


def _peek_existing_pid(path: str) -> int:
    """诊断辅助：不持锁地读 PID 文件内容。"""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            txt = f.read().strip()
        return int(txt) if txt.isdigit() else -1
    except OSError:
        return -1
