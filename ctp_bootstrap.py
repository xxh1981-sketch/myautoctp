"""注入 autotrade / autostraggle 代码路径（不修改原仓库）。"""

import os
import sys


def _resolve_root(env_key: str, config_val: str, default: str) -> str:
    env = os.environ.get(env_key, '').strip()
    if env and os.path.isdir(env):
        return os.path.abspath(env)
    if config_val and os.path.isdir(config_val):
        return os.path.abspath(config_val)
    if os.path.isdir(default):
        return os.path.abspath(default)
    return os.path.abspath(default)


def setup_paths(config: dict = None) -> tuple:
    """
    将 autotrade、autostraggle 加入 sys.path。
    顺序：先 autotrade（pairtrade / auto_*），再 autostraggle（straggle_*）。
    """
    config = config or {}
    merged = config.get('merged') or {}
    autotrade = _resolve_root(
        'AUTOTRADE_ROOT',
        merged.get('autotrade_root', ''),
        r'D:\autotrade',
    )
    autostraggle = _resolve_root(
        'AUTOSTRAGGLE_ROOT',
        merged.get('autostraggle_root', ''),
        r'D:\autostraggle',
    )
    for p in (autotrade, autostraggle):
        if not os.path.isdir(p):
            raise RuntimeError(f"目录不存在: {p}")
        if p not in sys.path:
            sys.path.insert(0, p)
    return autotrade, autostraggle


def _load_local_merged_config() -> dict:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'merged_config.yaml')
    if not os.path.isfile(path):
        return {}
    try:
        import yaml
        with open(path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


# 顶层 import straggle_* / auto_* 前必须先注入路径
setup_paths(_load_local_merged_config())
