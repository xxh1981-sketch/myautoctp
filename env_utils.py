"""Small environment / argv helpers."""

from __future__ import annotations

import os
import sys


def env_truthy(name: str) -> bool:
    return os.environ.get(name, '').strip().lower() in ('1', 'yes', 'true', 'y', 'ok')


def argv_has(flag: str) -> bool:
    return flag in sys.argv


def is_config_abs_path(path: str) -> bool:
    """配置路径是否为绝对路径（含 Windows 盘符路径在 Linux CI 上的识别）。"""
    if not path:
        return False
    s = str(path).strip()
    if os.path.isabs(s):
        return True
    if s.startswith('\\\\'):
        return True
    norm = s.replace('\\', '/')
    return len(norm) >= 3 and norm[1] == ':' and norm[0].isalpha() and norm[2] == '/'


def resolve_manual_start(config: dict) -> bool:
    """人工启动：显式 --manual / AUTOCTP_MANUAL；否则非进程内自动重启视为人工。"""
    if env_truthy('AUTOCTP_MANUAL') or argv_has('--manual'):
        return True
    if env_truthy('AUTOCTP_AUTO_RESTART') or argv_has('--auto-restart'):
        return False
    if config.get('_auto_restart'):
        return False
    return True
