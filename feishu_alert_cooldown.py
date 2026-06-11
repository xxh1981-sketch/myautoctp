"""Per-key Feishu alert cooldown (log every time, notify at most once per window).

Used by spread_close_ledger and feishu_noise_patch to throttle repetitive
operational alerts without silencing one-shot critical notifications.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Callable, Optional

_log = logging.getLogger(__name__)

_TS_PREFIX = '_feishu_alert_ts'
_DEFAULT_COOLDOWN_SEC = 300.0

# Substrings that must never be throttled (fills, connectivity, startup, risk_alerts).
_NO_THROTTLE_MARKERS = (
    'Fill Report',
    '成交回报',
    '系统健康检查异常',
    'CTP交易连接',
    'CTP行情连接',
    '每日报平安',
    '启动被拒绝',
    '启动自检告警',
    '启动保证金告警',
    '连续异常退出',
    '飞书暂停已生效',
    '重连隔离已持续',
    '对账 halt',
    '未完成入账',
    '保证金检查连续',
    '未配对腿持续无法消化',
    '单轮耗时过长',
    '达到每日交易限制',
)


def cooldown_sec(config: Optional[dict]) -> float:
    if not config:
        return _DEFAULT_COOLDOWN_SEC
    try:
        v = config.get('feishu_alert_cooldown_sec', _DEFAULT_COOLDOWN_SEC)
    except AttributeError:
        return _DEFAULT_COOLDOWN_SEC
    if v is None:
        return _DEFAULT_COOLDOWN_SEC
    return float(v)


def _state_bucket(conn=None, config: Optional[dict] = None) -> dict:
    if conn is not None:
        runtime = getattr(conn, '_runtime_state', None)
        if runtime is None:
            runtime = {}
            try:
                conn._runtime_state = runtime
            except Exception:
                return config.setdefault('_feishu_cooldown_bucket', {}) if config else {}
        return runtime
    if config is not None:
        return config.setdefault('_feishu_cooldown_bucket', {})
    return {}


def should_send(
    alert_key: str,
    *,
    conn=None,
    config: Optional[dict] = None,
    cooldown: Optional[float] = None,
) -> bool:
    """Return True if Feishu may be sent for this key; records timestamp on True."""
    if not alert_key:
        return True
    cd = cooldown if cooldown is not None else cooldown_sec(config)
    if cd <= 0:
        return True
    bucket = _state_bucket(conn, config)
    now = time.time()
    state_key = f'{_TS_PREFIX}:{alert_key}'
    last = float(bucket.get(state_key) or 0.0)
    if now - last < cd:
        return False
    bucket[state_key] = now
    return True


def send_message_cooldown(
    message: str,
    *,
    alert_key: str,
    config: Optional[dict] = None,
    conn=None,
    logger=None,
    cooldown: Optional[float] = None,
) -> bool:
    """Send Feishu if cooldown allows; return True if send attempted."""
    if not should_send(alert_key, conn=conn, config=config, cooldown=cooldown):
        if logger:
            logger.debug('[飞书冷却] 跳过重复告警 key=%s', alert_key)
        return False
    try:
        from auto_feishu import send_feishu_message
        return bool(send_feishu_message(message, config=config))
    except Exception as e:
        if logger:
            logger.warning('[飞书冷却] 发送失败 key=%s: %s', alert_key, e)
        return False


def _extract_symbol_bracket(message: str) -> str:
    m = re.search(r'\[([A-Za-z]+)\]', message)
    return (m.group(1) or '').lower() if m else ''


def _extract_field(message: str, label: str) -> str:
    m = re.search(rf'\*\*{re.escape(label)}\*\*:?\s*(\S+)', message)
    return (m.group(1) or '').strip() if m else ''


def throttle_key_for_message(message: str) -> Optional[str]:
    """Map repetitive operational messages to a cooldown key, or None to pass through."""
    if any(marker in message for marker in _NO_THROTTLE_MARKERS):
        return None
    if '流动性检查未通过' in message:
        sym = _extract_symbol_bracket(message)
        return f'spread_open_liq:{sym}' if sym else 'spread_open_liq:unknown'
    if '平仓整组流动性不足' in message:
        sym = _extract_field(message, '品种') or _extract_symbol_bracket(message)
        return f'spread_close_liq:{sym.lower()}' if sym else 'spread_close_liq:unknown'
    if '宽跨交易所拒单' in message:
        sym = _extract_field(message, '品种').lower()
        inst = _extract_field(message, '合约')
        if sym and inst:
            return f'strangle_reject:{sym}:{inst}'
        return f'strangle_reject:{sym or "unknown"}'
    if '宽跨期货价健全性未通过' in message:
        sym = _extract_field(message, '品种').lower()
        return f'strangle_parity:{sym}' if sym else 'strangle_parity:unknown'
    if '**品种**' in message and (
        '宽跨便宜腿' in message
        or ('宽跨' in message and '流动性' in message)
    ):
        sym = _extract_field(message, '品种').lower() or _extract_symbol_bracket(message)
        return f'strangle_liq:{sym}' if sym else 'strangle_liq:unknown'
    if '价差平仓不完整（账本）' in message:
        sym = _extract_field(message, '品种').lower()
        return f'spread_close_residual:{sym}' if sym else 'spread_close_residual:unknown'
    if '价差账本平仓后 A 残留' in message or '价差账本平仓后 A 仍有' in message:
        sym = _extract_symbol_bracket(message)
        return f'spread_close_a_residual:{sym}' if sym else 'spread_close_a_residual:unknown'
    return None


_ORIG_SEND: Optional[Callable] = None
_PATCHED = False


def install_send_feishu_throttle(config: Optional[dict] = None) -> bool:
    """Wrap auto_feishu.send_feishu_message with pattern-based cooldown (idempotent)."""
    global _ORIG_SEND, _PATCHED
    if _PATCHED:
        return True
    try:
        import auto_feishu
    except ImportError as e:
        _log.warning('[飞书降噪] auto_feishu 不可用，跳过 throttle 补丁: %s', e)
        return False

    _ORIG_SEND = auto_feishu.send_feishu_message

    def _patched_send(message, config=None, **kwargs):
        cfg = config or {}
        key = throttle_key_for_message(str(message))
        if key is None:
            return _ORIG_SEND(message, config=cfg, **kwargs)
        conn = cfg.get('_spread_fill_conn')
        if not should_send(key, conn=conn, config=cfg):
            return False
        return _ORIG_SEND(message, config=cfg, **kwargs)

    auto_feishu.send_feishu_message = _patched_send
    notifier_cls = getattr(auto_feishu, 'FeishuNotifier', None)
    if notifier_cls is not None and hasattr(notifier_cls, 'send_message'):
        _orig_notifier_send = notifier_cls.send_message

        def _patched_notifier_send(self, msg, msg_type='text'):
            key = throttle_key_for_message(str(msg))
            if key is None:
                return _orig_notifier_send(self, msg, msg_type=msg_type)
            cfg = getattr(self, 'config', None) or {}
            conn = cfg.get('_spread_fill_conn')
            if not should_send(key, conn=conn, config=cfg):
                return False
            return _orig_notifier_send(self, msg, msg_type=msg_type)

        notifier_cls.send_message = _patched_notifier_send

    _PATCHED = True
    _log.debug(
        '[飞书降噪] send_feishu_message throttle 已安装（冷却 %ss）',
        cooldown_sec(config),
    )
    return True
