"""Shared trade journal helpers (dedupe, daily shards, CTP direction mapping)."""

from __future__ import annotations

import glob
import json
import os
from datetime import date, timedelta
from typing import Set


# 合法的 journal_state 取值。仅 'pending'（待入账）与 'applied'（已入账，
# 含 skipped 类已决行）。任何其它值（拼写错误、空串、损坏）都不得被当成
# "已解决"——否则会误把未完成 pending 当作已应用，错误清除 journal_halt。
JOURNAL_STATE_PENDING = 'pending'
JOURNAL_STATE_APPLIED = 'applied'
_RESOLVED_JOURNAL_STATES = frozenset({JOURNAL_STATE_APPLIED})


def trade_dedupe_key(trade: dict) -> str:
    trade_id = (trade.get('trade_id') or '').strip()
    if trade_id:
        inst = (trade.get('instrument') or '').upper()
        return f'{inst}:{trade_id}'
    return '|'.join([
        str(trade.get('order_ref', '')),
        (trade.get('instrument') or '').upper(),
        str(trade.get('direction', '')),
        str(trade.get('offset', '')),
        str(trade.get('volume', '')),
        str(trade.get('price', '')),
        str(trade.get('trade_date', '')),
        str(trade.get('trade_time', '')),
    ])


def journal_daily_shards_enabled(config: dict = None) -> bool:
    dual = (config or {}).get('dual_strategy') or {}
    return bool(dual.get('journal_daily_shards', True))


def journal_retain_days(config: dict = None) -> int:
    dual = (config or {}).get('dual_strategy') or {}
    return max(1, int(dual.get('journal_retain_days', 14)))


def _shard_suffix(day: date) -> str:
    return day.strftime('-%Y%m%d')


def journal_path_for_day(base_path: str, day: date) -> str:
    root, ext = os.path.splitext(base_path)
    return f'{root}{_shard_suffix(day)}{ext or ".jsonl"}'


def active_journal_path(base_path: str, config: dict = None, day: date = None) -> str:
    """Return today's shard path when daily shards enabled, else base_path."""
    if not journal_daily_shards_enabled(config):
        return base_path
    return journal_path_for_day(base_path, day or date.today())


def _journal_glob_paths(base_path: str, config: dict = None) -> list:
    if not journal_daily_shards_enabled(config):
        return [base_path] if os.path.isfile(base_path) else []

    root, ext = os.path.splitext(base_path)
    pattern = f'{root}-*{ext or ".jsonl"}'
    retain = journal_retain_days(config)
    cutoff = date.today() - timedelta(days=retain - 1)
    paths = []
    for path in sorted(glob.glob(pattern)):
        name = os.path.basename(path)
        suffix = name[len(os.path.basename(root)):]
        if not suffix.startswith('-'):
            continue
        day_part = suffix[1:9]
        if not day_part.isdigit() or len(day_part) != 8:
            continue
        try:
            shard_day = date(int(day_part[:4]), int(day_part[4:6]), int(day_part[6:8]))
        except ValueError:
            continue
        if shard_day >= cutoff:
            paths.append(path)
    legacy = base_path if os.path.isfile(base_path) else None
    if legacy and legacy not in paths:
        paths.insert(0, legacy)
    return paths


def load_applied_keys(
    journal_base: str,
    config: dict = None,
    *,
    include_pending: bool = False,
) -> Set[str]:
    keys: Set[str] = set()
    for path in _journal_glob_paths(journal_base, config):
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                state = str(row.get('journal_state') or '').lower()
                if state == JOURNAL_STATE_PENDING:
                    if not include_pending:
                        continue
                elif state and state not in _RESOLVED_JOURNAL_STATES:
                    # 显式的未知/损坏状态：不视为已应用，避免据此抑制后续合法回放。
                    # 空串（遗留无 journal_state 字段的旧行）仍按已应用处理。
                    continue
                key = row.get('dedupe_key') or row.get('trade_id')
                if key:
                    keys.add(str(key))
    return keys


def _scan_journal(journal_base: str, config: dict = None) -> dict:
    """Single pass over retained shards.

    Returns a dict with:
      - ``pending_rows``: {key -> last pending row dict}
      - ``resolved_keys``: set of keys with a later applied/legacy row
      - ``malformed_lines`` / ``total_lines`` counters

    Shared by :func:`scan_unresolved_pending` (counts) and
    :func:`scan_unresolved_pending_rows` (the actual unresolved rows used by
    the self-healer). Keep both consumers in sync via this single scanner.
    """
    pending_rows: dict = {}
    resolved_keys: Set[str] = set()
    malformed_lines = 0
    total_lines = 0

    for path in _journal_glob_paths(journal_base, config):
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                total_lines += 1
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    malformed_lines += 1
                    continue
                state = str(row.get('journal_state') or '').lower()
                # 显式非空且既非 pending 也非已知"已解决"状态 → 损坏行，计入
                # malformed，且不得解除任何 pending（防止拼写错误状态误清未完成
                # 入账）。空串（遗留无 journal_state 的旧行）仍按已应用处理。
                if (
                    state
                    and state != JOURNAL_STATE_PENDING
                    and state not in _RESOLVED_JOURNAL_STATES
                ):
                    malformed_lines += 1
                    continue
                key = row.get('dedupe_key') or row.get('trade_id')
                if not key:
                    continue
                key = str(key)
                if state == JOURNAL_STATE_PENDING:
                    pending_rows[key] = row
                    continue
                resolved_keys.add(key)
    return {
        'pending_rows': pending_rows,
        'resolved_keys': resolved_keys,
        'malformed_lines': malformed_lines,
        'total_lines': total_lines,
    }


def scan_unresolved_pending_rows(
    journal_base: str,
    config: dict = None,
) -> list:
    """Return the actual unresolved pending row dicts (for the self-healer).

    A pending row is unresolved when no later applied/legacy row with the same
    dedupe key exists in retained shards.
    """
    scan = _scan_journal(journal_base, config)
    resolved = scan['resolved_keys']
    return [
        row for key, row in scan['pending_rows'].items()
        if key not in resolved
    ]


def scan_unresolved_pending(
    journal_base: str,
    config: dict = None,
) -> dict:
    """Return unresolved pending rows and malformed count for one journal.

    A pending row is unresolved when no later non-pending row with the same
    dedupe key exists in retained shards.
    """
    scan = _scan_journal(journal_base, config)
    pending_keys = set(scan['pending_rows'].keys())
    unresolved = pending_keys - scan['resolved_keys']
    malformed_lines = scan['malformed_lines']
    total_lines = scan['total_lines']
    return {
        'unresolved_pending': len(unresolved),
        'malformed_lines': malformed_lines,
        'total_lines': total_lines,
    }


def append_journal(journal_base: str, row: dict, config: dict = None) -> str:
    """Append one JSON line; returns path written."""
    from data_path_guard import guard_repo_data_write
    path = active_journal_path(journal_base, config)
    guard_repo_data_write(path)
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    with open(path, 'a', encoding='utf-8') as f:
        f.write(json.dumps(row, ensure_ascii=False) + '\n')
        f.flush()
        os.fsync(f.fileno())
    return path


def map_direction_offset(direction: str, offset: str) -> tuple:
    from pairtrade.constants import DIRECTION_BUY, DIRECTION_SELL, OFFSET_CLOSE, OFFSET_OPEN

    d = str(direction or '').strip()
    o = str(offset or '').strip()
    if not o or o == '?':
        o = OFFSET_OPEN
    direction_out = DIRECTION_BUY if d in ('0', DIRECTION_BUY) else DIRECTION_SELL
    offset_out = OFFSET_OPEN if o in ('0', OFFSET_OPEN) else OFFSET_CLOSE
    return direction_out, offset_out
