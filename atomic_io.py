"""Atomic file writes (temp + fsync + replace)."""

from __future__ import annotations

import os
import tempfile


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
        os.replace(tmp_path, path)
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
