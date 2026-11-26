"""无人值守可观测性：配置 drift 告警 + halt 解除通知。

纯通知，不改变 halt / 交易语义；失败仅打日志。
"""

from __future__ import annotations

import os
import time
from datetime import datetime
from typing import List

DEFAULT_CONFIG_DRIFT_CHECK_INTERVAL_SEC = 600.0
DEFAULT_HALT_RECOVERY_COOLDOWN_SEC = 300.0
DEFAULT_HV_STALE_WARN_DAYS = 7.0
DEFAULT_HV_STALE_CHECK_INTERVAL_SEC = 21600.0
DEFAULT_HV_STALE_ALERT_COOLDOWN_SEC = 86400.0

_HALT_TRACKERS = (
    ('margin', '_margin_halt_open', '保证金 halt'),
    ('journal', '_journal_halt_open', 'journal halt'),
    ('spread_reconcile', '_spread_reconcile_halt', '价差对账 halt'),
    ('strangle_reconcile', '_strangle_reconcile_halt', '宽跨对账 halt'),
    ('position_csv', '_position_csv_halt_open', '持仓 CSV 损坏 halt'),
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
        runtime['_config_drift_last_detected_at'] = now
        runtime['_config_drift_last_detected_mtime'] = current_mtime
        if logger:
            logger.warning('[配置] merged_config.yaml 已变更，已发送未重启提醒')
        return True

    if logger:
        logger.warning('[配置] merged_config.yaml drift 飞书发送未成功')
    return False


def maybe_alert_hv_staleness(conn, config: dict, logger=None) -> bool:
    """宽跨 HV 收盘价库（tradeinfo/futures.xlsx）过期 / 缺失飞书提醒。

    该库由用户手工维护；漏更新时宽跨平价溢价率会继续用旧 HV（缺失时静默
    回退 tradeinfo vol_basis）正常交易，没有任何 halt——这是唯一的提醒通道。
    以文件 mtime 为新鲜度代理（手工更新即刷新 mtime，免去 xlsx 解析依赖）。
    纯可观测性：只告警，不改变信号或交易语义。Returns True when alert sent.
    """
    str_cfg = config.get('strangle') or {}
    if not bool(str_cfg.get('hv_stale_alert_enabled', True)):
        return False
    warn_days = float(
        str_cfg.get('hv_stale_warn_days', DEFAULT_HV_STALE_WARN_DAYS) or 0,
    )
    if warn_days <= 0:
        return False

    runtime = _runtime(conn)
    now = time.time()
    interval = float(
        str_cfg.get(
            'hv_stale_check_interval_sec', DEFAULT_HV_STALE_CHECK_INTERVAL_SEC,
        ) or 0,
    )
    last_check = float(runtime.get('_hv_stale_last_check_at', 0.0) or 0.0)
    if interval > 0 and now - last_check < interval:
        return False
    runtime['_hv_stale_last_check_at'] = now

    path = str(str_cfg.get('hv_close_path') or '').strip()
    if not path:
        return False

    if not os.path.isfile(path):
        detail = f'HV 收盘价库不存在: `{path}`\n宽跨 sigma 将静默回退 tradeinfo vol_basis。'
        age_days = None
    else:
        try:
            mtime = os.path.getmtime(path)
        except OSError as e:
            if logger:
                logger.debug(f'[宽跨HV] 过期检查 mtime 失败: {e}')
            return False
        age_days = (now - mtime) / 86400.0
        if age_days < warn_days:
            runtime['_hv_stale_last_alert_ts'] = 0.0
            return False
        updated_at = datetime.fromtimestamp(mtime).isoformat(timespec='seconds')
        detail = (
            f'文件: `{path}`\n'
            f'最后更新: {updated_at}（约 {age_days:.1f} 天前，阈值 {warn_days:.0f} 天）\n'
            '宽跨平价溢价率仍在用旧 HV 计算建仓/平仓信号。'
        )

    cooldown = float(
        str_cfg.get(
            'hv_stale_alert_cooldown_sec', DEFAULT_HV_STALE_ALERT_COOLDOWN_SEC,
        ) or 0,
    )
    last_alert = float(runtime.get('_hv_stale_last_alert_ts', 0.0) or 0.0)
    if cooldown > 0 and now - last_alert < cooldown:
        return False

    body = (
        '⚠️ **宽跨 HV 收盘价库需要更新**\n\n'
        f'{detail}\n\n'
        '请更新 tradeinfo 收盘价库（无需重启，下次信号计算即读到新数据）。'
    )
    if logger:
        logger.warning(
            '[宽跨HV] 收盘价库%s（%s）',
            '缺失' if age_days is None else f'已 {age_days:.1f} 天未更新',
            path,
        )
    try:
        from auto_feishu import send_feishu_message
        ok = bool(send_feishu_message(body, config=config))
    except Exception as e:
        if logger:
            logger.warning(f'[宽跨HV] 过期飞书告警失败: {e}')
        return False
    if ok:
        runtime['_hv_stale_last_alert_ts'] = now
        return True
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
