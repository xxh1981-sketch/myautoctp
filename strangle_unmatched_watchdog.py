"""Watchdog for strangle unmatched legs stuck at max retry (e.g. CTP reject loop).

A leg whose ``b_retry_count`` has reached ``B_max_retries`` keeps cycling
through ``send_b_style_leg`` on every main-loop round. Two failure modes look
identical from outside:

1. Market is illiquid / not at opposite-side price yet — desirable, keep trying.
2. CTP keeps rejecting (no position to close, contract invalid, etc.) — a
   silent infinite loop that spams feishu reject alerts without progress.

This watchdog records the first time each leg reached saturation and emits a
*single* feishu alert after ``stuck_alert_age_sec`` so the operator can
investigate. It does not remove items from the unmatched queue automatically;
clearing is left to manual action (avoids deleting a still-live leg).
"""

from __future__ import annotations

import time
from typing import Any, Dict, Iterable, List, Tuple


STATE_FIRST_SEEN = '_strangle_stuck_first_seen'
STATE_LAST_ALERTED = '_strangle_stuck_last_alerted'
STATE_METADATA_LAST_ALERTED = '_strangle_unmatched_metadata_last_alerted'

DEFAULT_STUCK_ALERT_AGE_SEC = 600
DEFAULT_ALERT_COOLDOWN_SEC = 1800
DEFAULT_METADATA_ALERT_COOLDOWN_SEC = 1800


def _safe_float(value, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _safe_int(value, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _b_max(config: dict) -> int:
    str_cfg = (config.get('strangle') or {}) if config else {}
    default = _safe_int(str_cfg.get('phase2_max_retries', 10), 10)
    return _safe_int(config.get('B_max_retries', default), default)


def _watchdog_cfg(config: dict) -> Tuple[float, float]:
    str_cfg = (config.get('strangle') or {}) if config else {}
    age = _safe_float(
        str_cfg.get(
            'unmatched_stuck_alert_age_sec',
            DEFAULT_STUCK_ALERT_AGE_SEC,
        ),
        DEFAULT_STUCK_ALERT_AGE_SEC,
    )
    cooldown = _safe_float(
        str_cfg.get(
            'unmatched_stuck_alert_cooldown_sec',
            DEFAULT_ALERT_COOLDOWN_SEC,
        ),
        DEFAULT_ALERT_COOLDOWN_SEC,
    )
    return age, cooldown


def _metadata_alert_cfg(config: dict) -> Tuple[bool, float]:
    str_cfg = (config.get('strangle') or {}) if config else {}
    enabled = bool(str_cfg.get('unmatched_leg_metadata_alert', True))
    cooldown = _safe_float(
        str_cfg.get(
            'unmatched_leg_metadata_alert_cooldown_sec',
            DEFAULT_METADATA_ALERT_COOLDOWN_SEC,
        ),
        DEFAULT_METADATA_ALERT_COOLDOWN_SEC,
    )
    return enabled, cooldown


def _leg_key(item: dict) -> Tuple[str, str, str, str]:
    sym = (item.get('symbol') or '').lower()
    month = str(item.get('month') or '')
    kind = str(item.get('kind') or '')
    leg = item.get('leg') or {}
    inst = str(leg.get('inst') or item.get('filled_instrument') or '')
    if not inst:
        # 空 inst 时退化为按对象身份区分，避免多腿压缩成同一 key
        # 导致告警合并失真。
        inst = f'<unknown:{id(item)}>'
    return (sym, month, kind, inst)


def _metadata_key(index: int, item: dict) -> Tuple[Any, ...]:
    try:
        return _leg_key(item)
    except Exception:
        return (index, '<invalid>')


def validate_unmatched_leg_metadata(items: Iterable[dict]) -> List[Dict[str, Any]]:
    """Return unmatched leg items whose metadata may break downstream consumers."""
    bad: List[Dict[str, Any]] = []
    for index, item in enumerate(items or []):
        if not isinstance(item, dict):
            bad.append({'index': index, 'missing': ['item'], 'item': item})
            continue
        missing: List[str] = []
        for field in ('symbol', 'month', 'kind'):
            if not str(item.get(field) or '').strip():
                missing.append(field)
        leg = item.get('leg')
        if leg is not None and not isinstance(leg, dict):
            missing.append('leg must be dict')
        leg_inst = ''
        if isinstance(leg, dict):
            leg_inst = str(leg.get('inst') or '').strip()
        filled_inst = str(item.get('filled_instrument') or '').strip()
        if not leg_inst and not filled_inst:
            missing.append('leg.inst or filled_instrument')
        try:
            int(item.get('b_retry_count', 0))
        except Exception:
            missing.append('b_retry_count')
        if missing:
            bad.append({
                'index': index,
                'missing': missing,
                'key': _metadata_key(index, item),
                'item': item,
            })
    return bad


def _format_metadata_issue(issue: Dict[str, Any]) -> str:
    key = issue.get('key')
    missing = issue.get('missing') or []
    return f"index={issue.get('index')} key={key} missing={missing}"


def _send_metadata_alert(config, logger, bad_items: List[Dict[str, Any]]) -> None:
    try:
        from auto_feishu import send_feishu_message
    except Exception:
        send_feishu_message = None  # type: ignore

    lines = [_format_metadata_issue(item) for item in bad_items[:20]]
    body = (
        '⚠️ **宽跨未配对腿 metadata 异常**\n\n'
        f'发现 {len(bad_items)} 条 unmatched_legs 缺少关键字段，可能影响 close-only / holdback / 告警去重。\n\n'
        + '\n'.join(lines)
    )
    if logger:
        logger.warning(f'[宽跨守护] 检测到 {len(bad_items)} 条 unmatched_legs metadata 异常')
    if send_feishu_message is None:
        return
    try:
        send_feishu_message(body, config=config)
    except Exception as e:
        if logger:
            logger.warning(f'[宽跨守护] 飞书告警失败: {e}')


def _runtime_state(conn) -> dict:
    state = getattr(conn, '_runtime_state', None)
    if state is None:
        state = {}
        setattr(conn, '_runtime_state', state)
    return state


def _stuck_items(
    items: Iterable[dict], b_max: int,
) -> List[dict]:
    out: List[dict] = []
    for it in items:
        try:
            retry = int(it.get('b_retry_count', 0))
        except Exception:
            retry = 0
        if retry >= b_max:
            out.append(it)
    return out


def _send_alert(config, logger, leg_keys: List[Tuple[str, str, str, str]]) -> None:
    try:
        from auto_feishu import send_feishu_message
    except Exception:
        send_feishu_message = None  # type: ignore

    lines = []
    for sym, month, kind, inst in leg_keys[:20]:
        lines.append(f'- [{sym}] {month} {kind} {inst}')
    body = (
        f'⚠️ **宽跨未配对腿持续无法消化** ({len(leg_keys)} 条)\n\n'
        f'已达最大重试次数仍未成交，可能为 CTP 拒单 / 合约失效 / 流动性枯竭。\n'
        f'请人工检查 `straggle_ledger.json` 的 `unmatched_legs` 列表并酌情清理:\n'
        + '\n'.join(lines)
    )
    if logger:
        logger.warning(
            f'[宽跨守护] 检测到 {len(leg_keys)} 条 unmatched 腿持续卡住已达阈值'
        )
    if send_feishu_message is None:
        return
    try:
        send_feishu_message(body, config=config)
    except Exception as e:
        if logger:
            logger.warning(f'[宽跨守护] 飞书告警失败: {e}')


def check_unmatched_health(conn, ledger, config: dict, logger) -> None:
    """Inspect ledger.unmatched_legs and emit a single feishu alert when a leg
    has been stuck at ``b_retry_count == B_max_retries`` for ``stuck_alert_age_sec``.

    Safe to call on every main-loop round; internal cooldown prevents spam.
    """
    if ledger is None or not hasattr(ledger, 'list_unmatched_legs'):
        return

    try:
        items = ledger.list_unmatched_legs() or []
    except Exception:
        return

    runtime = _runtime_state(conn)
    enabled, cooldown = _metadata_alert_cfg(config)
    if enabled:
        bad_metadata = validate_unmatched_leg_metadata(items)
        last_alerted = runtime.setdefault(STATE_METADATA_LAST_ALERTED, {})
        now = time.time()
        if bad_metadata and now - float(last_alerted.get('all', 0.0) or 0.0) >= cooldown:
            last_alerted['all'] = now
            _send_metadata_alert(config, logger, bad_metadata)

    first_seen: Dict[Any, float] = runtime.setdefault(STATE_FIRST_SEEN, {})
    last_alerted: Dict[Any, float] = runtime.setdefault(STATE_LAST_ALERTED, {})

    age_threshold, cooldown = _watchdog_cfg(config)
    b_max = _b_max(config)
    now = time.time()

    stuck = _stuck_items(items, b_max)
    stuck_keys = {_leg_key(it) for it in stuck}

    for k in list(first_seen.keys()):
        if k not in stuck_keys:
            first_seen.pop(k, None)
            last_alerted.pop(k, None)

    alert_keys: List[Tuple[str, str, str, str]] = []
    for key in stuck_keys:
        first_ts = first_seen.get(key)
        if first_ts is None:
            first_seen[key] = now
            continue
        age = now - first_ts
        if age < age_threshold:
            continue
        last_alert = last_alerted.get(key, 0.0)
        if now - last_alert < cooldown:
            continue
        last_alerted[key] = now
        alert_keys.append(key)

    if alert_keys:
        _send_alert(config, logger, alert_keys)
