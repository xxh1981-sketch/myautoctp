"""Persist and merge CTP trades for cross-day / offline replay.

``query_trades_sync`` in autotrade returns the current trading-day snapshot
only. After a calendar-day disconnect, yesterday's fills may no longer appear
in CTP but can still exist in the on-disk cache (from prior OnRtnTrade or
query). Replay merges live query + retained cache, deduped by
:func:`trade_journal.trade_dedupe_key`.

Only trades with numeric CTP TradeID are cached or replayed from disk, so
unit-test mock ids (Q1/T1) cannot pollute production ledgers.
"""

from __future__ import annotations

import json
import os
from datetime import date, timedelta
from typing import List, Optional, Set

from trade_journal import is_plausible_ctp_trade, iter_trade_dedupe_keys


def _cache_path(config: dict) -> str:
    dual = config.get('dual_strategy') or {}
    path = dual.get(
        'trade_replay_cache',
        os.path.join('data', 'trade_replay_cache.jsonl'),
    )
    if not os.path.isabs(path):
        path = os.path.join(os.path.dirname(__file__), path)
    return path


def trade_replay_lookback_days(config: dict) -> int:
    dual = config.get('dual_strategy') or {}
    return max(0, int(dual.get('trade_replay_lookback_days', 1)))


def _trade_date_yyyymmdd(trade: dict) -> str:
    raw = str(trade.get('trade_date') or '').strip().replace('-', '')
    if len(raw) >= 8 and raw[:8].isdigit():
        return raw[:8]
    return date.today().strftime('%Y%m%d')


def _cutoff_yyyymmdd(lookback_days: int) -> str:
    return (date.today() - timedelta(days=lookback_days)).strftime('%Y%m%d')


def _guard_cache_write(path: str) -> None:
    from data_path_guard import guard_repo_data_write
    guard_repo_data_write(path)


def purge_invalid_cache_entries(
    config: dict,
    *,
    logger=None,
) -> int:
    """Drop non-plausible / expired rows from the replay cache file."""
    lookback = trade_replay_lookback_days(config)
    if lookback <= 0:
        return 0
    path = _cache_path(config)
    if not os.path.isfile(path):
        return 0
    cutoff = _cutoff_yyyymmdd(lookback)
    kept: List[dict] = []
    removed = 0
    try:
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    removed += 1
                    continue
                td = _trade_date_yyyymmdd(row)
                if td < cutoff or not is_plausible_ctp_trade(row):
                    removed += 1
                    if logger and not is_plausible_ctp_trade(row):
                        logger.warning(
                            '[trade_replay] 已从缓存移除非 CTP 成交 '
                            f'trade_id={row.get("trade_id")!r} '
                            f'instrument={row.get("instrument")}'
                        )
                    continue
                kept.append(row)
    except OSError as e:
        if logger:
            logger.debug(f'[trade_replay] 读缓存失败: {e}')
        return 0
    if removed <= 0:
        return 0
    from atomic_io import atomic_write_text
    _guard_cache_write(path)
    text = ''.join(
        json.dumps(row, ensure_ascii=False) + '\n' for row in kept
    )
    atomic_write_text(path, text)
    if logger:
        logger.info(f'[trade_replay] 缓存净化完成，移除 {removed} 条无效/过期成交')
    return removed


def record_trades_to_cache(
    trades: List[dict],
    config: dict,
    *,
    logger=None,
) -> int:
    """Append unseen trades to the replay cache. Returns newly recorded count."""
    if not trades:
        return 0
    lookback = trade_replay_lookback_days(config)
    if lookback <= 0:
        return 0

    path = _cache_path(config)
    existing: Set[str] = set()
    if os.path.isfile(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not is_plausible_ctp_trade(row):
                        continue
                    for k in iter_trade_dedupe_keys(row):
                        existing.add(k)
                    cached_key = row.get('dedupe_key')
                    if cached_key:
                        existing.add(str(cached_key))
        except OSError as e:
            if logger:
                logger.debug(f'[trade_replay] 读缓存失败: {e}')

    cutoff = _cutoff_yyyymmdd(lookback)
    new_count = 0
    _guard_cache_write(path)
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    try:
        with open(path, 'a', encoding='utf-8') as f:
            for trade in trades:
                if not is_plausible_ctp_trade(trade):
                    if logger:
                        logger.warning(
                            '[trade_replay] 跳过缓存非 CTP 成交 '
                            f'trade_id={trade.get("trade_id")!r} '
                            f'instrument={trade.get("instrument")}'
                        )
                    continue
                td = _trade_date_yyyymmdd(trade)
                if td < cutoff:
                    continue
                keys = list(iter_trade_dedupe_keys(trade))
                if any(k in existing for k in keys):
                    continue
                row = dict(trade)
                row['dedupe_key'] = keys[0]
                row['trade_date'] = td
                f.write(json.dumps(row, ensure_ascii=False) + '\n')
                existing.update(keys)
                new_count += 1
            f.flush()
            os.fsync(f.fileno())
    except OSError as e:
        if logger:
            logger.warning(f'[trade_replay] 写缓存失败: {e}')
        return 0
    return new_count


def load_cached_trades(
    config: dict,
    *,
    lookback_days: Optional[int] = None,
) -> List[dict]:
    lookback = trade_replay_lookback_days(config) if lookback_days is None else lookback_days
    if lookback <= 0:
        return []
    path = _cache_path(config)
    if not os.path.isfile(path):
        return []
    cutoff = _cutoff_yyyymmdd(lookback)
    out: List[dict] = []
    try:
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not is_plausible_ctp_trade(row):
                    continue
                td = _trade_date_yyyymmdd(row)
                if td < cutoff:
                    continue
                out.append(row)
    except OSError:
        return []
    return out


def merge_trades_for_replay(
    live: Optional[List[dict]],
    config: dict,
    *,
    logger=None,
) -> Optional[List[dict]]:
    """Merge CTP query result with on-disk replay cache; dedupe by trade key.

    ``live is None`` means the CTP query failed. Empty list means a successful
    query with no trades. Callers that must not rebuild ledgers from a failed
    query should treat ``None`` as skip.
    """
    lookback = trade_replay_lookback_days(config)
    query_failed = live is None
    live_list = list(live or [])
    if lookback > 0:
        purge_invalid_cache_entries(config, logger=logger)
        if not query_failed:
            record_trades_to_cache(live_list, config, logger=logger)
    if lookback <= 0:
        return None if query_failed else live_list

    seen: Set[str] = set()
    out: List[dict] = []

    def _add(trade: dict) -> None:
        keys = list(iter_trade_dedupe_keys(trade))
        cached_key = trade.get('dedupe_key')
        if cached_key:
            keys.append(str(cached_key))
        if any(k in seen for k in keys):
            return
        for k in keys:
            seen.add(k)
        out.append(trade)

    for trade in live_list:
        _add(trade)
    for trade in load_cached_trades(config, lookback_days=lookback):
        _add(trade)
    if not out and query_failed:
        return None
    return out


def query_trades_for_replay(conn, config: dict, *, logger=None) -> Optional[List[dict]]:
    """Query CTP trades and merge with replay cache."""
    if not hasattr(conn, 'query_trades_sync'):
        return merge_trades_for_replay([], config, logger=logger)
    live = conn.query_trades_sync(timeout=12, use_cache=False)
    return merge_trades_for_replay(live, config, logger=logger)
