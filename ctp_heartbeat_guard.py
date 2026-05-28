"""Inject ``OnHeartBeatWarning`` into CTP SPIs as an early-detection sentinel.

CTP only fires ``OnFrontDisconnected`` when the broker actively closes the
session; in real networks (NAT timeout, firewall silently dropping packets)
the TCP stays half-open and CTP never reports the break. The library *does*
deliver ``OnHeartBeatWarning(nTimeLapse)`` whenever the heartbeat gap exceeds
the warn threshold, so we use it as an earlier signal than waiting for
``query_fail_count`` to hit its threshold (which itself depends on a
synchronous query being issued — and the query cache may suppress those).

Behavior
--------
* Cooldown'd warning logs (default 60s).
* When ``nTimeLapse`` exceeds ``ctp_heartbeat_dead_threshold_sec`` (default
  90s) we call ``ReconnectManager.mark_connection_dead`` (TD) /
  ``OnFrontDisconnected(-9)`` (MD) to start a real reconnect.
* Idempotent install — safe to call multiple times in the same interpreter.
"""

from __future__ import annotations

import time
from typing import Optional

_INSTALLED = False
_INSTALL_ERROR: Optional[str] = None


def is_installed() -> bool:
    """Return True when the heartbeat handler is currently installed."""
    return _INSTALLED


def get_install_error() -> Optional[str]:
    """Return last install failure reason, or None when guard is installed."""
    return _INSTALL_ERROR


def _now() -> float:
    return time.time()


def _ensure_state(conn) -> dict:
    rt = getattr(conn, '_runtime_state', None)
    if rt is None:
        return {}
    state = rt.get('_heartbeat_state')
    if state is None:
        state = {
            'td_last_warn_ts': 0.0,
            'md_last_warn_ts': 0.0,
            'td_last_dead_ts': 0.0,
            'md_last_dead_ts': 0.0,
        }
        rt['_heartbeat_state'] = state
    return state


def _config_get(conn, key: str, default):
    cfg = getattr(conn, 'config', None) or {}
    try:
        v = cfg.get(key, default)
    except AttributeError:
        return default
    if v is None:
        return default
    return v


def _on_heartbeat_warning(self, nTimeLapse, *, channel: str) -> None:
    conn = getattr(self, 'conn', None)
    if conn is None:
        return
    state = _ensure_state(conn)
    if not state:
        return

    log_cooldown = float(_config_get(conn, 'ctp_heartbeat_log_cooldown_sec', 60))
    dead_threshold = float(
        _config_get(conn, 'ctp_heartbeat_dead_threshold_sec', 90)
    )
    dead_cooldown = float(
        _config_get(conn, 'ctp_heartbeat_dead_cooldown_sec', 120)
    )

    now = _now()
    warn_key = f'{channel}_last_warn_ts'
    dead_key = f'{channel}_last_dead_ts'
    if now - state.get(warn_key, 0.0) >= log_cooldown:
        state[warn_key] = now
        try:
            conn.logger.warning(
                f'[心跳告警] {channel.upper()} OnHeartBeatWarning '
                f'nTimeLapse={nTimeLapse}s (阈值 dead={dead_threshold:.0f}s)'
            )
        except Exception:
            pass

    try:
        lapse = float(nTimeLapse)
    except (TypeError, ValueError):
        return

    if lapse < dead_threshold:
        return
    if now - state.get(dead_key, 0.0) < dead_cooldown:
        return
    state[dead_key] = now

    if channel == 'td':
        mgr = getattr(conn, '_reconnect_mgr', None)
        if mgr is None:
            return
        try:
            conn.logger.error(
                f'[心跳告警] 交易心跳间隔 {lapse:.0f}s ≥ {dead_threshold:.0f}s，'
                '主动标记连接死亡并重连'
            )
        except Exception:
            pass
        try:
            mgr.mark_connection_dead()
        except Exception as e:
            try:
                conn.logger.warning(f'[心跳告警] mark_connection_dead 异常: {e}')
            except Exception:
                pass
    else:
        try:
            conn.logger.error(
                f'[心跳告警] 行情心跳间隔 {lapse:.0f}s ≥ {dead_threshold:.0f}s，'
                '主动触发行情前置断开回调'
            )
        except Exception:
            pass
        try:
            self.OnFrontDisconnected(-9)
        except Exception as e:
            try:
                conn.logger.warning(f'[心跳告警] 行情 OnFrontDisconnected 调用异常: {e}')
            except Exception:
                pass


def install_heartbeat_warning() -> bool:
    """Patch ``AutoTradingSpi`` and ``AutoMarketDataSpi`` with heartbeat handlers.

    Returns True when the handler is now active (either freshly installed or
    previously installed), False when installation failed. Inspect
    :func:`get_install_error` for the reason.
    """
    global _INSTALLED, _INSTALL_ERROR
    if _INSTALLED:
        return True

    try:
        import auto_trading_spi as ats
        import auto_market_data_spi as amds
    except Exception as e:
        _INSTALL_ERROR = f'import auto_trading_spi/auto_market_data_spi 失败: {e}'
        return False

    AutoTradingSpi = getattr(ats, 'AutoTradingSpi', None)
    AutoMarketDataSpi = getattr(amds, 'AutoMarketDataSpi', None)
    if AutoTradingSpi is None or AutoMarketDataSpi is None:
        _INSTALL_ERROR = (
            'AutoTradingSpi / AutoMarketDataSpi 未找到（autotrade 版本不兼容？）'
        )
        return False

    def td_handler(self, nTimeLapse):
        _on_heartbeat_warning(self, nTimeLapse, channel='td')

    def md_handler(self, nTimeLapse):
        _on_heartbeat_warning(self, nTimeLapse, channel='md')

    AutoTradingSpi.OnHeartBeatWarning = td_handler
    AutoMarketDataSpi.OnHeartBeatWarning = md_handler
    _INSTALLED = True
    _INSTALL_ERROR = None
    return True
