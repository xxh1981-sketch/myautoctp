"""无人值守可观测性：配置 drift 告警 + halt 解除通知。

纯通知，不改变 halt / 交易语义；失败仅打日志。
"""

from __future__ import annotations

import os
import time
from datetime import datetime
from typing import List, Optional

DEFAULT_CONFIG_DRIFT_CHECK_INTERVAL_SEC = 600.0
DEFAULT_HALT_RECOVERY_COOLDOWN_SEC = 300.0

_HALT_TRACKERS = (
    ('margin', '_margin_halt_open', '保证金 halt'),
    ('journal', '_journal_halt_open', 'journal halt'),
    ('spread_reconcile', '_spread_reconcile_halt', '价差对账 halt'),
    ('strangle_reconcile', '_strangle_reconcile_halt', '宽跨对账 halt'),
)


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


def init_config_drift_baseline(conn, config: dict, logger=None) -> None:
    """Record merged_config.yaml mtime at process / loop start."""
    from merged_config import merged_config_local_path

    runtime = _runtime(conn)
    path = merged_config_local_path()
    runtime['_config_drift_path'] = path
    try:
        runtime['_config_drift_baseline_mtime'] = (
            os.path.getmtime(path) if os.path.isfile(path) else 0.0
        )
    except OSError as e:
        runtime['_config_drift_baseline_mtime'] = 0.0
        if logger:
            logger.debug(f'[配置] 无法读取 baseline mtime: {e}')
    runtime.setdefault('_config_drift_last_check_at', 0.0)
    runtime.setdefault('_config_drift_last_notified_mtime', 0.0)


def maybe_alert_config_drift(conn, config: dict, logger=None) -> bool:
    """If merged_config.yaml changed on disk since start, send one Feishu alert.

    Returns True when an alert was sent this call.
    """
    if not bool(_cfg(config, 'config_drift_alert_enabled', True)):
        return False

    runtime = _runtime(conn)
    path = runtime.get('_config_drift_path')
    if not path:
        init_config_drift_baseline(conn, config, logger)
        path = runtime.get('_config_drift_path')
    if not path or not os.path.isfile(path):
        return False

    interval = float(
        _cfg(config, 'config_drift_check_interval_sec', DEFAULT_CONFIG_DRIFT_CHECK_INTERVAL_SEC),
    )
    now = time.time()
    last_check = float(runtime.get('_config_drift_last_check_at', 0.0) or 0.0)
    if interval > 0 and now - last_check < interval:
        return False
    runtime['_config_drift_last_check_at'] = now

    try:
        current_mtime = os.path.getmtime(path)
    except OSError as e:
        if logger:
            logger.debug(f'[配置] drift 检查 mtime 失败: {e}')
        return False

    baseline = float(runtime.get('_config_drift_baseline_mtime', 0.0) or 0.0)
    if current_mtime <= baseline:
        return False

    last_notified = float(runtime.get('_config_drift_last_notified_mtime', 0.0) or 0.0)
    if current_mtime <= last_notified:
        return False

    changed_at = datetime.fromtimestamp(current_mtime).isoformat(timespec='seconds')
    body = (
        '⚠️ **merged_config.yaml 已变更，进程未重启**\n\n'
        f'文件: `{path}`\n'
        f'磁盘修改时间: {changed_at}\n'
        f'进程启动时 mtime 基准: '
        f'{datetime.fromtimestamp(baseline).isoformat(timespec="seconds") if baseline else "未知"}\n\n'
        '当前仍在运行启动时加载的旧配置；请按需重启 merged_main.py 使变更生效。'
    )
    try:
        from auto_feishu import send_feishu_message
        ok = bool(send_feishu_message(body, config=config))
    except Exception as e:
        if logger:
            logger.warning(f'[配置] drift 飞书告警失败: {e}')
        return False

    if ok:
        runtime['_config_drift_last_notified_mtime'] = current_mtime
        if logger:
            logger.warning('[配置] merged_config.yaml 已变更，已发送未重启提醒')
        return True

    if logger:
        logger.warning('[配置] merged_config.yaml drift 飞书发送未成功')
    return False


def maybe_notify_halt_recoveries(conn, config: dict, logger=None) -> bool:
    """When any tracked halt flag clears (True→False), send a batched Feishu notice.

    Returns True when a notification was sent.
    """
    if not bool(_cfg(config, 'halt_recovery_notify_enabled', True)):
        return False

    runtime = _runtime(conn)
    current = {
        tracker_id: bool(runtime.get(runtime_key, False))
        for tracker_id, runtime_key, _label in _HALT_TRACKERS
    }
    prev = runtime.get('_halt_notify_prev')
    if prev is None:
        runtime['_halt_notify_prev'] = current
        return False

    recovered: List[str] = []
    for tracker_id, _runtime_key, label in _HALT_TRACKERS:
        if prev.get(tracker_id) and not current.get(tracker_id):
            recovered.append(label)

    runtime['_halt_notify_prev'] = current
    if not recovered:
        return False

    cooldown = float(
        _cfg(config, 'halt_recovery_alert_cooldown_sec', DEFAULT_HALT_RECOVERY_COOLDOWN_SEC),
    )
    last_sent = float(runtime.get('_halt_recovery_alert_ts', 0.0) or 0.0)
    now = time.time()
    if cooldown > 0 and now - last_sent < cooldown:
        if logger:
            logger.info(
                f'[halt恢复] 已解除: {", ".join(recovered)}（飞书冷却中，仅日志）'
            )
        return False

    body = (
        '💚 **AutoCTP halt 已解除**\n\n'
        + '\n'.join(f'- {name}' for name in recovered)
        + '\n\n新开判定已恢复（仍受 VIX/DTE/日限等常规门闸约束）。'
    )
    try:
        from auto_feishu import send_feishu_message
        ok = bool(send_feishu_message(body, config=config))
    except Exception as e:
        if logger:
            logger.warning(f'[halt恢复] 飞书通知失败: {e}')
        return False

    if ok:
        runtime['_halt_recovery_alert_ts'] = now
        if logger:
            logger.info(f'[halt恢复] 已通知: {", ".join(recovered)}')
        return True

    if logger:
        logger.warning('[halt恢复] 飞书发送未成功')
    return False
