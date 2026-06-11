"""Install Feishu noise-reduction patches (cooldown + legacy duplicate suppression)."""

from __future__ import annotations

import logging

_log = logging.getLogger(__name__)
_INSTALLED = False


def install_feishu_noise_patch(config: dict = None) -> bool:
    """Idempotent: throttle repetitive alerts + suppress duplicate legacy fill summaries."""
    global _INSTALLED
    if _INSTALLED:
        return True
    config = config or {}
    from feishu_alert_cooldown import install_send_feishu_throttle
    from trade_feishu_notify import install_unified_trade_feishu

    ok_throttle = install_send_feishu_throttle(config)
    install_unified_trade_feishu(config)
    _INSTALLED = True
    if ok_throttle:
        _log.debug('[飞书降噪] 补丁已安装')
    return ok_throttle
