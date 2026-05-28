"""启动确认指纹：记录确认时账本文件状态，防止改 CSV 后仍无人值守跳过确认。"""

from __future__ import annotations

import json
import os
from typing import Dict, List, Optional, Tuple

from atomic_io import atomic_write_text

_FINGERPRINT_VERSION = 1


def _project_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def _resolve_path(path: str) -> str:
    p = str(path or '').strip()
    if not p:
        return p
    if os.path.isabs(p):
        return p
    return os.path.join(_project_dir(), p)


def startup_ack_meta_path(config: dict) -> str:
    dual = config.get('dual_strategy') or {}
    ack = dual.get('startup_ack_file', 'data/position_startup_ack.txt')
    return _resolve_path(f'{ack}.meta.json')


def tracked_ledger_paths(config: dict) -> List[str]:
    """确认时需与 CTP 一起核对的本地账本路径（存在才记入指纹）。"""
    dual = config.get('dual_strategy') or {}
    if not dual.get('startup_ack_track_ledger_files', True):
        return []
    str_cfg = config.get('strangle') or {}
    paths = [
        dual.get('spread_positions_csv', 'data/spread_positions.csv'),
        dual.get('strangle_positions_csv', 'data/strangle_positions.csv'),
        str_cfg.get('ledger_path', 'data/ledger_strangle.json'),
    ]
    extra = dual.get('startup_ack_tracked_files') or []
    if isinstance(extra, (list, tuple)):
        paths.extend(extra)
    seen = set()
    resolved: List[str] = []
    for raw in paths:
        p = _resolve_path(str(raw))
        if p and p not in seen:
            seen.add(p)
            resolved.append(p)
    return resolved


def _file_stat_signature(path: str) -> Optional[Dict[str, int]]:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return {'mtime_ns': int(st.st_mtime_ns), 'size': int(st.st_size)}


def build_ledger_fingerprint(config: dict) -> Dict[str, Dict[str, int]]:
    files: Dict[str, Dict[str, int]] = {}
    for path in tracked_ledger_paths(config):
        sig = _file_stat_signature(path)
        if sig is not None:
            files[path] = sig
    return files


def save_startup_ack_fingerprint(config: dict) -> None:
    dual = config.get('dual_strategy') or {}
    if not dual.get('startup_ack_track_ledger_files', True):
        return
    from datetime import date

    meta_path = startup_ack_meta_path(config)
    parent = os.path.dirname(meta_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    payload = {
        'version': _FINGERPRINT_VERSION,
        'confirmed_date': date.today().isoformat(),
        'files': build_ledger_fingerprint(config),
    }
    atomic_write_text(
        meta_path,
        json.dumps(payload, ensure_ascii=False, indent=2) + '\n',
    )


def load_startup_ack_fingerprint(config: dict) -> Optional[dict]:
    meta_path = startup_ack_meta_path(config)
    if not os.path.isfile(meta_path):
        return None
    try:
        with open(meta_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def remove_startup_ack_fingerprint(config: dict) -> None:
    meta_path = startup_ack_meta_path(config)
    if os.path.isfile(meta_path):
        try:
            os.remove(meta_path)
        except OSError:
            pass


def check_startup_ack_fingerprint(config: dict) -> Tuple[bool, List[str]]:
    """
    Returns (ok, reasons). ok=False 时禁止无人值守复用 position_startup_ack.txt。
    """
    dual = config.get('dual_strategy') or {}
    if not dual.get('startup_ack_track_ledger_files', True):
        return True, []

    stored = load_startup_ack_fingerprint(config)
    if stored is None:
        return False, ['无确认指纹文件(.meta.json)，可能为升级后首次自动重启或指纹被删']

    stored_files = stored.get('files')
    if not isinstance(stored_files, dict) or not stored_files:
        return False, ['确认指纹为空或损坏']

    reasons: List[str] = []
    for path, old_sig in stored_files.items():
        if not isinstance(old_sig, dict):
            reasons.append(f'{os.path.basename(path)}: 指纹条目无效')
            continue
        cur = _file_stat_signature(path)
        if cur is None:
            reasons.append(f'{os.path.basename(path)}: 确认后已删除或不可读')
            continue
        if cur.get('mtime_ns') != old_sig.get('mtime_ns') or cur.get('size') != old_sig.get('size'):
            reasons.append(f'{os.path.basename(path)}: 文件已在确认后修改')

    for path in tracked_ledger_paths(config):
        if path not in stored_files and _file_stat_signature(path) is not None:
            reasons.append(
                f'{os.path.basename(path)}: 确认时不存在、现已新增（需重新确认）',
            )

    return (len(reasons) == 0), reasons


def invalidate_startup_ack_files(config: dict) -> List[str]:
    """删除启动确认相关持久化文件；返回已删除路径列表。"""
    from account_decomposition import external_ack_path, remove_external_ack_file

    dual = config.get('dual_strategy') or {}
    removed: List[str] = []
    ack = _resolve_path(dual.get('startup_ack_file', 'data/position_startup_ack.txt'))
    meta = startup_ack_meta_path(config)
    ext = _resolve_path(external_ack_path(config))
    for path in (ack, meta, ext):
        if path and os.path.isfile(path):
            try:
                os.remove(path)
                removed.append(path)
            except OSError:
                pass
    remove_external_ack_file(config)
    remove_startup_ack_fingerprint(config)
    return removed
