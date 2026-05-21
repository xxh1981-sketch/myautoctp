"""Shared trade journal helpers (dedupe, daily shards, CTP direction mapping)."""

from __future__ import annotations

import glob
import json
import os
from datetime import date, timedelta
from typing import Set



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


def load_applied_keys(journal_base: str, config: dict = None) -> Set[str]:
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
                key = row.get('dedupe_key') or row.get('trade_id')
                if key:
                    keys.add(str(key))
    return keys


def append_journal(journal_base: str, row: dict, config: dict = None) -> str:
    """Append one JSON line; returns path written."""
    path = active_journal_path(journal_base, config)
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
