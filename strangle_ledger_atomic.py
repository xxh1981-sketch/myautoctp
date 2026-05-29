"""Patch StrangleLedger._save to use atomic write (kill-safe)."""

from __future__ import annotations

import json
import os

from atomic_io import atomic_write_text

_INSTALLED = False
_INSTALL_ERROR: str = ''


def get_install_error() -> str:
    """Last install failure reason (empty when install succeeded)."""
    return _INSTALL_ERROR


def install_atomic_save() -> bool:
    """Replace ``StrangleLedger._save`` so kill/power-off does not corrupt JSON.

    Returns ``True`` on success (or already installed). On failure returns
    ``False`` and records the reason in :func:`get_install_error`; the caller
    must surface this rather than silently fall back to the non-atomic original
    ``_save`` (a kill mid-write could then corrupt the ledger JSON).
    """
    global _INSTALLED, _INSTALL_ERROR
    if _INSTALLED:
        return True

    try:
        import straggle_ledger as sl
    except Exception as e:
        _INSTALL_ERROR = f'import straggle_ledger 失败: {e}'
        return False

    StrangleLedger = getattr(sl, 'StrangleLedger', None)
    if StrangleLedger is None:
        _INSTALL_ERROR = 'straggle_ledger 缺少 StrangleLedger 符号'
        return False

    def _atomic_save(self) -> None:
        os.makedirs(os.path.dirname(self.path) or '.', exist_ok=True)
        payload = json.dumps(self._data, ensure_ascii=False, indent=2)
        atomic_write_text(self.path, payload)

    StrangleLedger._save = _atomic_save
    _INSTALLED = True
    _INSTALL_ERROR = ''
    return True
