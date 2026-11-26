"""启动确认指纹：记录确认时账本文件状态，防止改 CSV 后仍无人值守跳过确认。"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Dict, List, Optional, Tuple

from atomic_io import atomic_write_text

# v2: JSON 账本改用「持仓真相字段」规范化内容哈希，避免运行期 cooldown/日限/
# halt 标志、未配对腿重试计数等非持仓写入反复触发「确认后已修改」误报。
_FINGERPRINT_VERSION = 2

# 宽跨 JSON 账本中真正代表持仓真相、需与 CTP 核对的字段；写入前会经
# ``_ledger_truth_payload`` 规范化（剔除 b_retry_count / combo_id 等运行期元数据）。
_LEDGER_TRUTH_FIELDS = ('positions', 'leg_claims', 'unmatched_legs')


def _norm_symbol(value) -> str:
    return str(value or '').strip().lower()


def _norm_inst(value) -> str:
    return str(value or '').strip().upper()


def _normalize_leg_claims(claims) -> Dict[str, int]:
    if not isinstance(claims, dict):
        return {}
    out: Dict[str, int] = {}
    for inst, vol in claims.items():
        key = _norm_inst(inst)
        if not key:
            continue
        try:
            iv = int(vol)
        except (TypeError, ValueError):
            continue
        if iv > 0:
            out[key] = out.get(key, 0) + iv
    return out


def _normalize_position_for_fingerprint(pos: dict) -> dict:
    if not isinstance(pos, dict):
        return {}
    return {
        'symbol': _norm_symbol(pos.get('symbol')),
        'month': str(pos.get('month') or ''),
        'call_instrument': _norm_inst(pos.get('call_instrument')),
        'put_instrument': _norm_inst(pos.get('put_instrument')),
        'call_strike': float(pos.get('call_strike') or 0),
        'put_strike': float(pos.get('put_strike') or 0),
        'call_volume': int(pos.get('call_volume') or 0),
        'put_volume': int(pos.get('put_volume') or 0),
        'groups': int(pos.get('groups') or 1),
        'status': str(pos.get('status') or ''),
    }


def _normalize_positions_list(positions) -> list:
    items = [
        _normalize_position_for_fingerprint(p)
        for p in (positions or [])
        if isinstance(p, dict)
    ]
    return sorted(
        items,
        key=lambda p: (
            p['symbol'], p['month'],
            p['call_instrument'], p['put_instrument'],
            p['groups'], p['status'],
            p['call_volume'], p['put_volume'],
        ),
    )


def _normalize_unmatched_for_fingerprint(items) -> list:
    out = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        leg = item.get('leg') or {}
        inst = leg.get('inst') or item.get('filled_instrument') or ''
        out.append({
            'symbol': _norm_symbol(item.get('symbol')),
            'month': str(item.get('month') or ''),
            'volume': int(item.get('volume') or 0),
            'kind': str(item.get('kind') or ''),
            'inst': _norm_inst(inst),
            'stage': str(item.get('stage') or ''),
            'action': str(item.get('action') or ''),
        })
    return sorted(
        out,
        key=lambda x: (x['symbol'], x['month'], x['inst'], x['kind'], x['stage']),
    )


def _ledger_truth_payload(data: dict) -> dict:
    """持仓真相规范化快照，供指纹哈希与单测复用。"""
    return {
        'positions': _normalize_positions_list(data.get('positions')),
        'leg_claims': _normalize_leg_claims(data.get('leg_claims')),
        'unmatched_legs': _normalize_unmatched_for_fingerprint(data.get('unmatched_legs')),
    }


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


def _ledger_content_signature(path: str) -> Optional[Dict[str, str]]:
    """宽跨 JSON 账本：仅按持仓真相字段做内容哈希。

    解析失败（缺失/损坏）返回 None，交由调用方回退到 stat 签名，使
    「文件被删/损坏」等真实异常仍能被检测为变更。
    """
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    truth = _ledger_truth_payload(data)
    canon = json.dumps(truth, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return {'content_sha': hashlib.sha256(canon.encode('utf-8')).hexdigest()}


def _is_json_ledger(path: str) -> bool:
    return str(path).lower().endswith('.json')


def _file_signature(path: str) -> Optional[dict]:
    """JSON 账本用持仓真相内容哈希；其余（CSV 等）用 mtime+size。"""
    if _is_json_ledger(path):
        sig = _ledger_content_signature(path)
        if sig is not None:
            return sig
    return _file_stat_signature(path)


def build_ledger_fingerprint(config: dict) -> Dict[str, dict]:
    files: Dict[str, dict] = {}
    for path in tracked_ledger_paths(config):
        sig = _file_signature(path)
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
        cur = _file_signature(path)
        if cur is None:
            reasons.append(f'{os.path.basename(path)}: 确认后已删除或不可读')
            continue
        if cur != old_sig:
            reasons.append(f'{os.path.basename(path)}: 文件已在确认后修改')

    for path in tracked_ledger_paths(config):
        if path not in stored_files and _file_signature(path) is not None:
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
