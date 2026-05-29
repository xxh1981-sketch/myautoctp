"""周末（双休日）非交易抑制补丁。

背景
----
``auto_scheduled_pause`` 的挂起窗口是**纯时刻**判断，无 weekday 感知；
``auto_scheduled_reconnect.check_scheduled_full_recovery`` 也只按 09/13/21 时刻
重连、**不查** ``is_connection_suspended``。于是周六、周日里程序仍会按时重连
CTP（市场已休市），登录失败 → 隔离/重试 churn + 告警噪音。

本补丁只处理**双休日**（用户决策：法定节假日除非可自动获取否则当交易日，不处理）：

- 包住 ``auto_scheduled_pause.is_connection_suspended``：周末返回 True，使
  ``sync_connection_suspend_state`` 主动释放连接、健康检查降噪、主循环走离线跳过。
- 包住 ``auto_scheduled_reconnect.check_scheduled_full_recovery``：周末直接返回
  False（不触发定时重连）。

避免误杀周五夜盘
----------------
中国期货夜盘最晚约 02:30 结束，归属周五交易。若把"整个周六"判为周末会在周六
凌晨断开仍活跃的周五夜盘连接。故**周六**仅在 ``weekend_pause_saturday_from_hour``
（默认 5 点，安全晚于所有夜盘收盘）之后才算周末；周六凌晨仍由原挂起窗口逻辑接管。
**周日**全天无交易（无周日夜盘）→ 全天抑制；周一由原工作日窗口 + 09:00 定时重连
正常恢复。

补丁安装失败不致命：仅退回"周末照常尝试重连"的旧行为（噪音，非安全问题）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

_INSTALLED = False
_INSTALL_ERROR: Optional[str] = None
_orig_is_suspended = None
_orig_check_recovery = None


def is_installed() -> bool:
    return _INSTALLED


def get_install_error() -> Optional[str]:
    return _INSTALL_ERROR


def is_weekend_nontrading(config: dict, now: datetime = None) -> bool:
    """是否处于"双休日非交易"区间。

    周六：仅 ``weekend_pause_saturday_from_hour`` 时之后（避开周五夜盘跨零点）。
    周日：全天。其余工作日：False（交由原挂起窗口逻辑）。
    """
    cfg = config or {}
    if not cfg.get('weekend_pause_enabled', True):
        return False
    now = now or datetime.now()
    weekday = now.weekday()  # 周一=0 .. 周六=5, 周日=6
    if weekday == 6:
        return True
    if weekday == 5:
        sat_from = int(cfg.get('weekend_pause_saturday_from_hour', 5))
        return now.hour >= sat_from
    return False


def patched_is_connection_suspended(config, now=None):
    if is_weekend_nontrading(config, now):
        return True
    return _orig_is_suspended(config, now)


def patched_check_scheduled_full_recovery(conn, config, logger):
    if is_weekend_nontrading(config):
        rt = getattr(conn, '_runtime_state', None)
        if isinstance(rt, dict) and not rt.get('_weekend_pause_logged'):
            rt['_weekend_pause_logged'] = True
            try:
                logger.info('[周末非交易] 双休日，暂停定时重连与扫描（周一恢复）')
            except Exception:
                pass
        return False
    rt = getattr(conn, '_runtime_state', None)
    if isinstance(rt, dict):
        rt.pop('_weekend_pause_logged', None)
    return _orig_check_recovery(conn, config, logger)


def install_weekend_pause() -> bool:
    """包住 autotrade 的挂起判定与定时重连，使其感知双休日。

    返回 True 表示已生效（含此前已安装）；False 表示安装失败，原因见
    :func:`get_install_error`。幂等，可重复调用。
    """
    global _INSTALLED, _INSTALL_ERROR, _orig_is_suspended, _orig_check_recovery
    if _INSTALLED:
        return True
    try:
        import auto_scheduled_pause as asp
        import auto_scheduled_reconnect as asr
    except Exception as e:
        _INSTALL_ERROR = f'import auto_scheduled_pause/auto_scheduled_reconnect 失败: {e}'
        return False

    if not hasattr(asp, 'is_connection_suspended') or not hasattr(
        asr, 'check_scheduled_full_recovery',
    ):
        _INSTALL_ERROR = (
            'is_connection_suspended / check_scheduled_full_recovery 未找到'
            '（autotrade 版本不兼容？）'
        )
        return False

    _orig_is_suspended = asp.is_connection_suspended
    _orig_check_recovery = asr.check_scheduled_full_recovery
    asp.is_connection_suspended = patched_is_connection_suspended
    asr.check_scheduled_full_recovery = patched_check_scheduled_full_recovery
    _INSTALLED = True
    _INSTALL_ERROR = None
    return True
