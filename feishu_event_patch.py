"""安装飞书长连接（事件订阅）指令接收补丁。

不修改 ``D:\\autotrade``：以 monkeypatch 替换
``auto_feishu_command.start_command_receiver``，并把实例写回模块级
``_receiver``，保证 ``is_trading_paused()`` 读取的是同一实例。

仅在 ``notifications.feishu_command.mode == 'ws'`` 时生效；默认 ``poll``
保持原轮询行为，便于灰度切换与回退。
"""

from __future__ import annotations

import logging

_log = logging.getLogger('AutoCTP')
_INSTALLED = False
_INSTALL_ERROR = ''


def _feishu_cmd_config(config: dict) -> dict:
    return ((config or {}).get('notifications') or {}).get('feishu_command') or {}


def install_feishu_event_receiver(config: dict = None) -> bool:
    """Idempotent：切到长连接模式（mode=ws）时替换接收器工厂。"""
    global _INSTALLED, _INSTALL_ERROR
    if _INSTALLED:
        return True
    if _feishu_cmd_config(config).get('mode', 'poll') != 'ws':
        return True
    try:
        import auto_feishu_command as mod
        from feishu_event_receiver import FeishuEventReceiver

        original_start = mod.start_command_receiver

        def _start(cfg=None):
            cfg = cfg or {}
            if _feishu_cmd_config(cfg).get('mode', 'ws') != 'ws':
                return original_start(cfg)
            # 与轮询实现一致：进程内单例，活跃时不重复建立长连接；
            # 但若上一轮因异常退出调用过 stop_command_receiver()，须重新启动。
            existing = getattr(mod, '_receiver', None)
            if isinstance(existing, FeishuEventReceiver):
                if existing.enabled and not existing.is_running():
                    existing.start()
                return existing
            receiver = FeishuEventReceiver(cfg)
            mod._receiver = receiver
            if receiver.enabled:
                receiver.start()
            return receiver

        mod.start_command_receiver = _start
        _INSTALLED = True
        _log.info('[飞书指令] 已切换为长连接事件订阅模式')
        return True
    except Exception as e:
        _INSTALL_ERROR = str(e)
        _log.error('[飞书指令] 长连接补丁安装失败: %s', e)
        return False


def get_install_error() -> str:
    return _INSTALL_ERROR
