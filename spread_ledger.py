"""Spread strategy leg claims (signed net volume per instrument)."""

from __future__ import annotations

import re
import threading
from typing import Dict, Optional

from auto_connection import extract_symbol_prefix


class SpreadLegStore:
    """
    Lightweight spread ownership map.

    Volume is signed net position attributed to spread:
      positive = long (typically A-leg calls)
      negative = short (typically B-leg calls)
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._claims: Dict[str, int] = {}

    def set_leg_claims(self, claims: Dict[str, int]) -> None:
        with self._lock:
            self._claims = {
                str(inst).strip(): int(vol)
                for inst, vol in (claims or {}).items()
                if str(inst).strip() and int(vol) != 0
            }

    def list_leg_claims(self) -> Dict[str, int]:
        with self._lock:
            return dict(self._claims)

    def apply_delta(self, instrument: str, delta: int) -> None:
        if not instrument or delta == 0:
            return
        inst = str(instrument).strip()
        with self._lock:
            new_vol = int(self._claims.get(inst, 0)) + int(delta)
            if new_vol == 0:
                self._claims.pop(inst, None)
            else:
                self._claims[inst] = new_vol

    @staticmethod
    def _is_call_instrument(instrument: str) -> bool:
        inst = (instrument or '').strip().upper()
        if not inst:
            return False
        if re.search(r'[-]?C[-]?\d', inst):
            return True
        prefix = extract_symbol_prefix(inst)
        if not prefix:
            return False
        tail = inst[len(prefix):].upper()
        return bool(re.search(r'C\d', tail))

    def long_call_volumes(self) -> Dict[str, int]:
        """Positive long call volumes owned by spread (for strangle reconcile)."""
        with self._lock:
            out: Dict[str, int] = {}
            for inst, vol in self._claims.items():
                if vol > 0 and self._is_call_instrument(inst):
                    out[inst] = vol
            return out

    def short_call_volumes(self) -> Dict[str, int]:
        """Absolute short call volumes owned by spread."""
        with self._lock:
            out: Dict[str, int] = {}
            for inst, vol in self._claims.items():
                if vol < 0 and self._is_call_instrument(inst):
                    out[inst] = -vol
            return out


def store_from_conn(conn) -> Optional[SpreadLegStore]:
    runtime = getattr(conn, '_runtime_state', None) or {}
    store = runtime.get('_spread_leg_store')
    return store if isinstance(store, SpreadLegStore) else None
