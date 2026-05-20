"""Per-journal-file locks for idempotent trade apply (single-process)."""

from __future__ import annotations

import threading

_LOCKS_GUARD = threading.Lock()
_JOURNAL_LOCKS: dict[str, threading.RLock] = {}


def journal_lock(journal_file: str) -> threading.RLock:
    path = journal_file or ''
    with _LOCKS_GUARD:
        lock = _JOURNAL_LOCKS.get(path)
        if lock is None:
            lock = threading.RLock()
            _JOURNAL_LOCKS[path] = lock
        return lock
