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

    生产环境（默认）若目录不存在则立即抛错——线上启动绝不能跑空依赖。
    若设置环境变量 ``AUTOCTP_ALLOW_MISSING_DEPS=1``（CI / 本地无 autotrade
    源码环境用），则缺失目录时仅打印一行 warning 并继续；后续依赖 autotrade
    的 import 会自然 ImportError，由 pytest 用
    ``--continue-on-collection-errors`` 统一跳过。
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
    allow_missing = os.environ.get('AUTOCTP_ALLOW_MISSING_DEPS', '').strip() in (
        '1', 'true', 'True', 'yes',
    )
    for p in (autotrade, autostraggle):
        if not os.path.isdir(p):
            if allow_missing:
                sys.stderr.write(
                    f'[ctp_bootstrap] WARNING: 目录不存在 {p} '
                    '(AUTOCTP_ALLOW_MISSING_DEPS=1，已跳过)\n'
                )
                continue
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
