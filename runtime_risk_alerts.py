"""Runtime alerts for residual risks (feishu pause, margin unknown, quarantine, reconcile halt).

All helpers are safe to call every main-loop round; internal cooldowns prevent spam.
Does not change halt / trading semantics — notification only.
"""

from __future__ import annotations

import time
from typing import List, Tuple

from strangle_rebalance_close_only import CLOSE_KINDS

DEFAULT_ALERT_COOLDOWN_SEC = 1800
DEFAULT_MARGIN_UNKNOWN_ALERT_AFTER = 3
DEFAULT_QUARANTINE_ALERT_AFTER_SEC = 600


def _runtime(conn) -> dict:
    state = getattr(conn, '_runtime_state', None)
    if state is None:
        state = {}
        setattr(conn, '_runtime_state', state)
    return state


def _cfg(config: dict, key: str, default):
    if config is None:
        return default
    v = config.get(key, default)
    return default if v is None else v


def _alert_cooldown(config: dict) -> float:
    return float(_cfg(config, 'risk_alert_cooldown_sec', DEFAULT_ALERT_COOLDOWN_SEC))


def _can_send(runtime: dict, state_key: str, cooldown: float) -> bool:
    now = time.time()
    last = float(runtime.get(state_key) or 0.0)
    if now - last < cooldown:
        return False
    runtime[state_key] = now
    return True


def _send_feishu(body: str, config: dict, logger, log_prefix: str) -> None:
    if logger:
        logger.warning(f'{log_prefix} {body.split(chr(10))[0][:120]}')
    try:
        from auto_feishu import send_feishu_message
        send_feishu_message(body, config=config)
    except Exception as e:
        if logger:
            logger.debug(f'{log_prefix} 飞书失败: {e}')


def _open_unmatched_items(ledger) -> List[dict]:
    if ledger is None or not hasattr(ledger, 'list_unmatched_legs'):
        return []
    try:
        items = ledger.list_unmatched_legs() or []
    except Exception:
        return []
    return [it for it in items if it.get('kind') not in CLOSE_KINDS]


def _exposure_summary(conn, ledger) -> Tuple[bool, str]:
    """Return (has_exposure, short detail for alert body)."""
    runtime = _runtime(conn)
    lines: List[str] = []

    open_unmatched = _open_unmatched_items(ledger)
    if open_unmatched:
        lines.append(f'宽跨开仓类未配对腿 {len(open_unmatched)} 条（含 awaiting_phase2）')

    if runtime.get('_strangle_reconcile_halt'):
        issues = list(runtime.get('_strangle_reconcile_issues') or [])[:3]
        lines.append('宽跨对账 halt' + (f': {"; ".join(issues)}' if issues else ''))

    if runtime.get('_spread_reconcile_halt'):
        issues = list(runtime.get('_spread_reconcile_issues') or [])[:3]
        lines.append('价差对账 halt' + (f': {"; ".join(issues)}' if issues else ''))

    if runtime.get('_margin_halt_open'):
        reason = runtime.get('_margin_halt_reason') or '保证金超限'
        lines.append(f'保证金 halt: {reason}')

    if conn._reconnect_quarantine or not getattr(conn, 'td_logined', True):
        lines.append('CTP 重连隔离中')

    if not lines:
        return False, ''
    return True, '\n'.join(f'- {ln}' for ln in lines)


def notify_feishu_pause_exposure(
    conn, ledger, config: dict, logger, *, paused: bool,
) -> None:
    """On transition into feishu pause, warn if exposure / halt / unmatched exists."""
    if not _cfg(config, 'feishu_pause_exposure_alert', True):
        return
    runtime = _runtime(conn)
    was = bool(runtime.get('_feishu_pause_active'))
    runtime['_feishu_pause_active'] = paused
    if not paused or was:
        return

    has, detail = _exposure_summary(conn, ledger)
    if not has:
        return
    cooldown = _alert_cooldown(config)
    if not _can_send(runtime, '_feishu_pause_exposure_alert_ts', cooldown):
        return
    body = (
        '⚠️ **飞书暂停已生效，自动交易全停（含平仓扫描）**\n\n'
        '当前存在以下敞口/风控状态，暂停期间不会自动处理，请确认是否接受：\n'
        f'{detail}\n\n'
        '恢复交易：飞书恢复指令；紧急平仓请人工介入。'
    )
    _send_feishu(body, config, logger, '[风控告警]')


def record_margin_check_result(conn, config: dict, logger, status: str) -> None:
    """Track consecutive margin ``unknown`` and alert after threshold."""
    runtime = _runtime(conn)
    if status == 'unknown':
        streak = int(runtime.get('_margin_unknown_streak') or 0) + 1
        runtime['_margin_unknown_streak'] = streak
    else:
        runtime['_margin_unknown_streak'] = 0
        return

    threshold = int(
        _cfg(config, 'margin_unknown_alert_after', DEFAULT_MARGIN_UNKNOWN_ALERT_AFTER),
    )
    if streak < threshold:
        return
    cooldown = _alert_cooldown(config)
    if not _can_send(runtime, '_margin_unknown_alert_ts', cooldown):
        return
    prev_halt = bool(runtime.get('_margin_halt_open', False))
    prev_reason = runtime.get('_margin_halt_reason') or '无'
    body = (
        f'⚠️ **保证金检查连续 {streak} 次无法判定（unknown）**\n\n'
        f'沿用上一轮 halt={prev_halt}，reason={prev_reason}。\n'
        '请检查 CTP 连接/持仓查询；长时间 unknown 可能导致风控状态滞后。'
    )
    _send_feishu(body, config, logger, '[风控告警]')


def notify_quarantine_prolonged(conn, config: dict, logger) -> None:
    """Alert when reconnect quarantine exceeds configured duration."""
    runtime = _runtime(conn)
    in_q = bool(getattr(conn, '_reconnect_quarantine', False))
    now = time.time()
    since_key = '_quarantine_since_ts'

    if not in_q:
        runtime.pop(since_key, None)
        return

    since = runtime.get(since_key)
    if since is None:
        runtime[since_key] = now
        return

    try:
        elapsed = now - float(since)
    except (TypeError, ValueError):
        return

    threshold = float(
        _cfg(config, 'quarantine_alert_after_sec', DEFAULT_QUARANTINE_ALERT_AFTER_SEC),
    )
    if elapsed < threshold:
        return

    cooldown = _alert_cooldown(config)
    if not _can_send(runtime, '_quarantine_prolonged_alert_ts', cooldown):
        return

    td_ok = getattr(conn, 'td_logined', False)
    md_ok = getattr(conn, 'md_logined', False)
    body = (
        f'⚠️ **CTP 重连隔离已持续 {elapsed:.0f}s**（阈值 {threshold:.0f}s）\n\n'
        f'TD登录={td_ok} MD登录={md_ok}；此期间不扫策略、不自动平仓。\n'
        '若长时间不恢复，请检查网络/柜台或人工处理敞口。'
    )
    _send_feishu(body, config, logger, '[风控告警]')


def notify_reconcile_halt_open_unmatched(
    conn, ledger, config: dict, logger,
) -> None:
    """Alert when strangle reconcile halt blocks open-class unmatched legs."""
    if not _cfg(config, 'reconcile_halt_open_unmatched_alert', True):
        return
    runtime = _runtime(conn)
    if not runtime.get('_strangle_reconcile_halt'):
        return

    open_items = _open_unmatched_items(ledger)
    if not open_items:
        return

    cooldown = _alert_cooldown(config)
    if not _can_send(runtime, '_reconcile_halt_open_unmatched_alert_ts', cooldown):
        return

    lines = []
    for it in open_items[:15]:
        sym = (it.get('symbol') or '').upper()
        month = it.get('month', '')
        kind = it.get('kind', '')
        leg = it.get('leg') or {}
        inst = leg.get('inst') or it.get('filled_instrument') or ''
        lines.append(f'- [{sym}] {month} {kind} {inst}')
    issues = list(runtime.get('_strangle_reconcile_issues') or [])[:3]
    body = (
        '⚠️ **宽跨对账 halt：开仓类未配对腿无法自动补第二腿**\n\n'
        + (f'对账: {"; ".join(issues)}\n' if issues else '')
        + f'共 {len(open_items)} 条（close_chp_pending 仍会处理）：\n'
        + '\n'.join(lines)
        + '\n\n请先修复账本/CTP 一致性或人工平仓；解除 halt 前存在裸腿窗口。'
    )
    _send_feishu(body, config, logger, '[风控告警]')
