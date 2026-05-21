"""Crash-safe holdback file for ``run_close_only_rebalance``.

Background
----------
``strangle_rebalance_close_only.run_close_only_rebalance`` (used during margin
halt) needs to ask the strangle executor to process ONLY ``close_chp_pending``
unmatched legs while ``awaiting_phase2`` (open second-leg) items are kept out
of the way.

The previous implementation moved non-close items out of ``ledger.unmatched_legs``
and put them back after the executor returned. That worked while the process
stayed alive, but a kill / power loss between the two steps would leave
``ledger_strangle.json`` containing only the close items — the
``awaiting_phase2`` legs would be permanently lost on restart, leaving naked
short options open.

Design
------
We persist ``other_items`` into an independent file
``data/_close_only_holdback.json`` *before* mutating the ledger:

1. ``begin``: atomic write holdback file with ``other_items``.
2. caller mutates ``ledger.unmatched_legs`` and runs the executor.
3. ``end``: caller restores the ledger and we remove the holdback file.

If the process crashes between (1) and (3), the next startup calls
``recover_holdback_into_ledger`` which merges the holdback back into the
ledger and removes the file. Merge is dedup'ed by leg key so a partial
recovery (e.g. some legs already added by the runtime) is idempotent.
"""

from __future__ import annotations

import json
import os
from typing import List, Optional

from atomic_io import atomic_write_text


_HOLDBACK_BASENAME = '_close_only_holdback.json'


def _ledger_dir(ledger) -> str:
    path = getattr(ledger, 'path', '') or ''
    return os.path.dirname(os.path.abspath(path)) or '.'


def holdback_path(ledger) -> str:
    """Path to the holdback file (sibling to ledger json)."""
    return os.path.join(_ledger_dir(ledger), _HOLDBACK_BASENAME)


def _leg_key(item: dict) -> tuple:
    """Tuple identifying a unmatched leg for dedupe across crash recovery."""
    sym = (item.get('symbol') or '').lower()
    month = str(item.get('month') or '')
    kind = str(item.get('kind') or '')
    leg = item.get('leg') or {}
    inst = str(leg.get('inst') or item.get('filled_instrument') or '')
    return (sym, month, kind, inst)


def begin_holdback(ledger, other_items: List[dict]) -> Optional[str]:
    """Persist ``other_items`` to holdback file. Returns path on success."""
    if not other_items:
        return None
    path = holdback_path(ledger)
    payload = {'items': list(other_items)}
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))
    return path


def end_holdback(ledger) -> None:
    """Remove the holdback file (called after executor returned and ledger restored)."""
    path = holdback_path(ledger)
    if os.path.isfile(path):
        try:
            os.remove(path)
        except OSError:
            pass


def _safe_remove(path: str, logger=None) -> None:
    try:
        os.remove(path)
    except OSError as e:
        if logger:
            logger.debug(f'[宽跨守护] 删除 holdback 文件失败({path}): {e}')


def recover_holdback_into_ledger(ledger, logger=None) -> int:
    """
    Merge holdback file (if exists) back into ``ledger.unmatched_legs``.

    Called once on program startup, BEFORE the main loop starts. If the prior
    process crashed during a close-only rebalance, this restores the
    ``awaiting_phase2`` (and other non-close) legs so they are not silently
    dropped.

    Returns number of items merged.

    **Crash safety**:
      - ``ledger._save()`` failure → 文件 **保留**，下次启动再次尝试合并
        （合并是基于 ``_leg_key`` 的幂等去重，重复无害）；异常向上抛出由
        主程序处理，**不静默吞掉**。
      - 文件读取失败 / payload 为空 → 删除文件（无可恢复数据）。
      - 任何其他异常都不会让 ``added`` 进入未定义状态：``added`` 在最外层
        初始化为 0。
    """
    path = holdback_path(ledger)
    if not os.path.isfile(path):
        return 0

    try:
        with open(path, 'r', encoding='utf-8') as f:
            payload = json.load(f) or {}
    except Exception as e:
        if logger:
            logger.warning(
                f'[宽跨守护] holdback 文件读取失败({path}): {e}, 已忽略'
            )
        _safe_remove(path, logger)
        return 0

    items = payload.get('items') or []
    if not items:
        _safe_remove(path, logger)
        return 0

    added = 0
    save_ok = False
    lock = getattr(ledger, '_lock', None)
    if lock is not None:
        lock.acquire()
    try:
        existing = list(ledger._data.get('unmatched_legs') or [])
        existing_keys = {_leg_key(it) for it in existing}
        merged = list(existing)
        for it in items:
            if _leg_key(it) in existing_keys:
                continue
            merged.append(it)
            added += 1
        if added:
            ledger._data['unmatched_legs'] = merged
            try:
                ledger._save()
            except Exception as e:
                # 保留 holdback 文件让下次启动重试；异常向上传递。
                if logger:
                    logger.error(
                        f'[宽跨守护] holdback 合并写盘失败({path}): {e}，'
                        '保留文件以便下次启动重试合并。'
                    )
                raise
            save_ok = True
        else:
            # 全部已在 ledger 中：没有写盘需求，但也算"恢复完成"。
            save_ok = True
    finally:
        if lock is not None:
            lock.release()

    # 只有真正完成（写盘成功或无需写盘）才删除文件
    if save_ok:
        _safe_remove(path, logger)

    if logger:
        if added:
            logger.warning(
                f'[宽跨守护] 自 holdback 恢复 {added} 条 unmatched 腿 '
                f'(文件: {path})。可能是上次 close_only 期间崩溃。'
            )
        else:
            logger.info(
                f'[宽跨守护] holdback 文件存在但项均已在 ledger 中，已删除: {path}'
            )
    return added
