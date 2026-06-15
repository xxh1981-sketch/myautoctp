"""运维维护模式：继续监控/对账/告警，禁止一切自动发单与撤单。

与飞书暂停不同：
  - 飞书暂停：主循环 skip 策略扫描（零自动动作）
  - 维护模式：主循环照常跑，在发单/撤单层全局拦截

维护模式不触发自动平仓或撤单 sweep；进程退出时 shutdown_cancel 仍生效。
"""

from __future__ import annotations

import os
from typing import Optional

_GUARD_INSTALLED = False


def _project_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def _resolve_flag_path(config: dict) -> str:
    path = str(config.get('maintenance_mode_file') or 'data/maintenance_mode.flag')
    if not os.path.isabs(path):
        path = os.path.join(_project_dir(), path)
    return path


def is_maintenance_mode(config: dict = None) -> bool:
    """True when config flag or on-disk flag file is set."""
    config = config or {}
    if bool(config.get('maintenance_mode', False)):
        return True
    path = _resolve_flag_path(config)
    return os.path.isfile(path)


def should_block_trading(conn, config: dict = None) -> bool:
    """Block automatic sends/cancels during maintenance (not during shutdown)."""
    if not is_maintenance_mode(config or getattr(conn, 'config', None) or {}):
        return False
    try:
        from shutdown_cancel import is_shutdown_cancel_active
        if is_shutdown_cancel_active(conn):
            return False
    except Exception:
        pass
    return True


def install_maintenance_guard(config: dict = None) -> bool:
    """Patch send_order / close send / cancel paths once per process."""
    global _GUARD_INSTALLED
    if _GUARD_INSTALLED:
        return True
    try:
        import auto_order_manager as aom
    except ImportError:
        return False

    original = aom.OrderManager.send_order
    if not getattr(original, '_maintenance_wrapped', False):

        def guarded_send_order(self, instrument, *args, **kwargs):
            conn = self.conn
            cfg = getattr(conn, 'config', None) or {}
            if should_block_trading(conn, cfg):
                self.logger.info(
                    f'[维护模式] 拒绝发单: {instrument}',
                )
                return None, None
            return original(self, instrument, *args, **kwargs)

        guarded_send_order._maintenance_wrapped = True  # type: ignore[attr-defined]
        aom.OrderManager.send_order = guarded_send_order

    try:
        import auto_closer_executor as ace
        orig_send = ace._send_and_wait
        if not getattr(orig_send, '_maintenance_wrapped', False):

            def guarded_send_and_wait(
                conn, instrument, direction, volume, price, offset,
                timeout, config, logger, symbol,
                base_future_price: float = None,
                price_change_threshold: float = None,
                strategy: str = 'spread',
            ):
                if should_block_trading(conn, config):
                    logger.info(
                        f'[维护模式] 拒绝发单: {symbol} {instrument}',
                    )
                    return False, 0, 0.0
                return orig_send(
                    conn, instrument, direction, volume, price, offset,
                    timeout, config, logger, symbol,
                    base_future_price=base_future_price,
                    price_change_threshold=price_change_threshold,
                    strategy=strategy,
                )

            guarded_send_and_wait._maintenance_wrapped = True  # type: ignore[attr-defined]
            ace._send_and_wait = guarded_send_and_wait
    except ImportError:
        pass

    _GUARD_INSTALLED = True
    return True


def wrap_connection_cancel_guard(conn, config: dict = None) -> None:
    """Per-connection cancel guard (conn class lives in autotrade)."""
    if getattr(conn, '_maintenance_cancel_wrapped', None) is True:
        return
    original = getattr(conn, 'cancel_all_pending_orders', None)
    if original is None or not callable(original):
        return
    if getattr(original, '_maintenance_wrapped', None) is True:
        conn._maintenance_cancel_wrapped = True
        return

    def guarded_cancel(*args, **kwargs):
        cfg = config or getattr(conn, 'config', None) or {}
        if should_block_trading(conn, cfg):
            logger = getattr(conn, 'logger', None)
            if logger:
                logger.info('[维护模式] 拒绝自动撤单')
            return 0
        return original(*args, **kwargs)

    guarded_cancel._maintenance_wrapped = True  # type: ignore[attr-defined]
    conn.cancel_all_pending_orders = guarded_cancel
    conn._maintenance_cancel_wrapped = True


def maybe_notify_maintenance_enabled(conn, config: dict, logger=None) -> None:
    """One-shot Feishu notice when maintenance becomes active."""
    runtime = getattr(conn, '_runtime_state', None)
    if not isinstance(runtime, dict):
        return
    active = is_maintenance_mode(config)
    was = bool(runtime.get('_maintenance_mode_active'))
    runtime['_maintenance_mode_active'] = active
    if not active or was:
        return
    if logger:
        logger.warning('[维护模式] 已生效：继续监控，禁止一切自动发单/撤单')
    try:
        from auto_feishu import send_feishu_message
        send_feishu_message(
            '🔧 **AutoCTP 维护模式已生效**\n\n'
            '主循环继续查询 CTP/CSV 与告警，但已禁止一切自动发单、撤单、'
            '平仓与再平衡。\n\n'
            '解除：关闭 `maintenance_mode` 或删除 flag 文件后重启/下轮生效。',
            config=config,
        )
    except Exception as e:
        if logger:
            logger.warning(f'[维护模式] 飞书通知失败: {e}')
