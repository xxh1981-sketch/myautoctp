"""Small environment / argv helpers."""

from __future__ import annotations

import os
import sys


def env_truthy(name: str) -> bool:
    return os.environ.get(name, '').strip().lower() in ('1', 'yes', 'true', 'y', 'ok')


def argv_has(flag: str) -> bool:
    return flag in sys.argv


def resolve_manual_start(config: dict) -> bool:
    """人工启动：显式 --manual / AUTOCTP_MANUAL；否则非进程内自动重启视为人工。"""
    if env_truthy('AUTOCTP_MANUAL') or argv_has('--manual'):
        return True
    if env_truthy('AUTOCTP_AUTO_RESTART') or argv_has('--auto-restart'):
        return False
    if config.get('_auto_restart'):
        return False
    return True
