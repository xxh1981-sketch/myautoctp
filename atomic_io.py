"""Atomic file writes (temp + fsync + replace)."""

from __future__ import annotations

import os
import tempfile
import time

# Windows 上 os.replace 偶发 PermissionError [WinError 5]：目标文件被杀软扫描或
# 并发读短暂占用时，原子重命名被拒。这是瞬时争用，短重试即可，不必放弃原子写语义
# （实盘曾导致 ledger_strangle.json 保存抛未捕获异常、中断当轮宽跨处理）。
_REPLACE_RETRIES = 5
_REPLACE_RETRY_SLEEP = 0.05


def _replace_with_retry(tmp_path: str, path: str) -> None:
    for attempt in range(_REPLACE_RETRIES):
        try:
            os.replace(tmp_path, path)
            return
        except PermissionError:
            if attempt + 1 >= _REPLACE_RETRIES:
                raise
            time.sleep(_REPLACE_RETRY_SLEEP * (attempt + 1))


def atomic_write_bytes(path: str, data: bytes) -> None:
    from data_path_guard import guard_repo_data_write
    guard_repo_data_write(path)
    directory = os.path.dirname(os.path.abspath(path)) or '.'
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix='.tmp_', dir=directory)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        _replace_with_retry(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def atomic_write_text(path: str, text: str, encoding: str = 'utf-8') -> None:
    atomic_write_bytes(path, text.encode(encoding))


def atomic_write_text_with_newline(path: str, text: str, encoding: str = 'utf-8') -> None:
    payload = text if text.endswith('\n') else text + '\n'
    atomic_write_text(path, payload, encoding=encoding)
