"""Run only the close-leg branch of ``StrangleExecutor`` rebalance.

Used during margin halt: full ``run_rebalance`` would process
``awaiting_phase2`` items (open-leg phase 2), increasing risk while we are
trying to *reduce* exposure. ``close_chp_pending`` items are the second leg
of an in-flight close — leaving them stuck means a naked option remains
open. We therefore want to keep clearing those even while margin is over the
limit.

Implementation strategy: temporarily move non-close items out of the ledger
queue, invoke the executor's normal ``run_rebalance``, then restore them.
This avoids depending on private APIs of ``StrangleExecutor``.

Crash safety
------------
Between the "move out" and "restore" steps the executor runs WITHOUT the
ledger lock (we must release it so OnRtnTrade etc. can update other ledger
fields). If the process is killed in that window the ``awaiting_phase2``
legs would be permanently lost on disk — naked option risk.

We therefore persist the moved-out items to ``_close_only_holdback.json``
*before* mutating the ledger; ``recover_holdback_into_ledger`` (called on
program startup) merges them back if the file is found. Once the executor
returns and we put the items back into the ledger, the holdback file is
removed.
"""

from __future__ import annotations

from strangle_close_only_holdback import (
    begin_holdback,
    end_holdback,
)

CLOSE_KINDS = {'close_chp_pending'}


def _leg_key(item: dict) -> tuple:
    sym = (item.get('symbol') or '').lower()
    month = str(item.get('month') or '')
    kind = str(item.get('kind') or '')
    leg = item.get('leg') or {}
    inst = str(leg.get('inst') or item.get('filled_instrument') or '')
    return (sym, month, kind, inst)


def run_close_only_rebalance(executor, ledger, tradeinfo_by_key) -> int:
    """Process only close-side unmatched legs; return number actually handled."""
    if ledger is None or executor is None:
        return 0
    if not hasattr(ledger, 'list_unmatched_legs'):
        return 0

    all_items = list(ledger.list_unmatched_legs() or [])
    if not all_items:
        return 0

    close_items = [it for it in all_items if it.get('kind') in CLOSE_KINDS]
    other_items = [it for it in all_items if it.get('kind') not in CLOSE_KINDS]

    if not close_items:
        return 0

    if not other_items:
        before = len(close_items)
        executor.run_rebalance(tradeinfo_by_key)
        after = len(ledger.list_unmatched_legs() or [])
        return max(0, before - after)

    # Persist the held-back non-close items BEFORE mutating the ledger so a
    # crash in the executor leaves a recoverable trail on disk.
    begin_holdback(ledger, other_items)

    other_keys = {_leg_key(it) for it in other_items}
    close_keys = {_leg_key(it) for it in close_items}

    lock = getattr(ledger, '_lock', None)
    if lock is not None:
        lock.acquire()
    try:
        ledger._data['unmatched_legs'] = list(close_items)
        ledger._save()
    finally:
        if lock is not None:
            lock.release()

    try:
        executor.run_rebalance(tradeinfo_by_key)
        if lock is not None:
            lock.acquire()
        try:
            current = list(ledger._data.get('unmatched_legs') or [])
            # Items currently in the ledger that originated as "close" — these
            # are the close items not yet consumed by the executor.
            remaining_close = [it for it in current if _leg_key(it) in close_keys]
            # Items added during the executor run by other threads (OnRtnTrade
            # chain) — keep them.
            new_during_run = [
                it for it in current
                if _leg_key(it) not in close_keys
                and _leg_key(it) not in other_keys
            ]
            handled = max(0, len(close_items) - len(remaining_close))
            ledger._data['unmatched_legs'] = (
                remaining_close + other_items + new_during_run
            )
            ledger._save()
        finally:
            if lock is not None:
                lock.release()
        end_holdback(ledger)
        return handled
    except Exception:
        if lock is not None:
            lock.acquire()
        try:
            current = list(ledger._data.get('unmatched_legs') or [])
            remaining_close = [it for it in current if _leg_key(it) in close_keys]
            new_during_run = [
                it for it in current
                if _leg_key(it) not in close_keys
                and _leg_key(it) not in other_keys
            ]
            ledger._data['unmatched_legs'] = (
                remaining_close + other_items + new_during_run
            )
            ledger._save()
        finally:
            if lock is not None:
                lock.release()
        end_holdback(ledger)
        raise
