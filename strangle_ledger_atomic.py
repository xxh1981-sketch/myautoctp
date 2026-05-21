"""Patch StrangleLedger._save to use atomic write (kill-safe)."""

from __future__ import annotations

import json
import os

from atomic_io import atomic_write_text

_INSTALLED = False


def install_atomic_save() -> None:
    """Replace ``StrangleLedger._save`` so kill/power-off does not corrupt JSON."""
    global _INSTALLED
    if _INSTALLED:
        return

    try:
        import straggle_ledger as sl
    except Exception:
        return

    StrangleLedger = getattr(sl, 'StrangleLedger', None)
    if StrangleLedger is None:
        return

    def _atomic_save(self) -> None:
        os.makedirs(os.path.dirname(self.path) or '.', exist_ok=True)
        payload = json.dumps(self._data, ensure_ascii=False, indent=2)
        atomic_write_text(self.path, payload)

    StrangleLedger._save = _atomic_save
    _INSTALLED = True
