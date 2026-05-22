"""Account-level check: CTP signed net = spread claims + strangle long claims (+ external)."""

from __future__ import annotations

import json
import os
from datetime import date
from typing import Dict, List, Optional, Set, Tuple

from atomic_io import atomic_write_text
from spread_derive import query_ctp_signed_positions
from spread_position_adjust import merge_strangle_owned_volumes

_EXTERNAL_ACK_VERSION = 1


def normalize_inst_map(claims: Dict[str, int]) -> Dict[str, int]:
    """Merge leg claims by upper-case instrument id."""
    out: Dict[str, int] = {}
    for inst, vol in (claims or {}).items():
        key = str(inst).strip().upper()
        if not key:
            continue
        out[key] = out.get(key, 0) + int(vol)
    return out


def get_acknowledged_external(config: dict) -> Dict[str, int]:
    if not config.get('_external_positions_acknowledged'):
        return {}
    return dict(config.get('_startup_external_positions') or {})


def external_ack_path(config: dict) -> str:
    dual = config.get('dual_strategy') or {}
    custom = dual.get('external_positions_ack_file')
    if custom:
        return str(custom)
    ack = dual.get('startup_ack_file', 'data/position_startup_ack.txt')
    base_dir = os.path.dirname(ack) or 'data'
    return os.path.join(base_dir, 'external_positions_ack.json')


def _external_ack_persist_enabled(config: dict) -> bool:
    return bool((config.get('dual_strategy') or {}).get('external_ack_persist', True))


def _external_ack_require_today(config: dict) -> bool:
    return bool((config.get('dual_strategy') or {}).get('external_ack_require_today', False))


def external_maps_match(a: Dict[str, int], b: Dict[str, int]) -> bool:
    return normalize_inst_map(a) == normalize_inst_map(b)


def save_external_ack_file(config: dict, external: Dict[str, int]) -> None:
    """Write confirmed external positions next to startup ack (atomic JSON)."""
    normalized = normalize_inst_map(external)
    path = external_ack_path(config)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    payload = {
        'version': _EXTERNAL_ACK_VERSION,
        'confirmed_date': date.today().isoformat(),
        'positions': {k: int(v) for k, v in normalized.items() if int(v) != 0},
    }
    atomic_write_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2) + '\n',
    )


def remove_external_ack_file(config: dict) -> None:
    path = external_ack_path(config)
    if os.path.isfile(path):
        try:
            os.remove(path)
        except OSError:
            pass


def load_external_ack_file(config: dict) -> Optional[Dict[str, int]]:
    """
    Load persisted external positions.

    Returns None if missing/invalid/stale (require_today); {} if file exists with
    empty positions; otherwise normalized instrument -> signed volume map.
    """
    path = external_ack_path(config)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if _external_ack_require_today(config):
        if data.get('confirmed_date') != date.today().isoformat():
            return None
    positions = data.get('positions')
    if not isinstance(positions, dict):
        return None
    return {
        k: int(v)
        for k, v in normalize_inst_map(positions).items()
        if int(v) != 0
    }


def register_acknowledged_external(
    config: dict,
    external: Dict[str, int],
    conn=None,
    *,
    persist: bool = True,
) -> None:
    """Register external-position ack for periodic reconcile; optionally persist JSON."""
    normalized = {
        k: int(v) for k, v in normalize_inst_map(external).items() if int(v) != 0
    }
    config['_startup_external_positions'] = normalized
    config['_external_positions_acknowledged'] = bool(normalized)
    runtime = getattr(conn, '_runtime_state', None) if conn is not None else None
    if runtime is not None:
        runtime['_startup_external_positions'] = normalized
        runtime['_external_positions_acknowledged'] = bool(normalized)
    if persist and _external_ack_persist_enabled(config):
        if normalized:
            save_external_ack_file(config, normalized)
        else:
            remove_external_ack_file(config)


def restore_external_ack_from_file(
    config: dict,
    conn,
    ledger,
    logger=None,
    *,
    strict: Optional[bool] = None,
) -> bool:
    """
    Unattended startup: load JSON, verify against live decomposition, register.

    When strict (default from config external_ack_strict_on_restore):
    - mismatch or live external gap without JSON -> False
    - CTP query failed -> False
    """
    if strict is None:
        strict = bool(
            (config.get('dual_strategy') or {}).get(
                'external_ack_strict_on_restore', True,
            )
        )
    stored = load_external_ack_file(config)
    path = external_ack_path(config)
    has_file = os.path.isfile(path)

    if conn is None:
        if stored and strict:
            if logger:
                logger.error(
                    '[启动] 存在外部仓 JSON 但无 CTP 连接，无法校验，拒绝无人值守启动'
                )
            return False
        if stored:
            register_acknowledged_external(
                config, stored, conn, persist=False,
            )
            if logger:
                logger.info(
                    f'[启动] 已从 JSON 恢复外部仓登记 ({len(stored)} 个合约，未校验 CTP)'
                )
        return True

    from spread_ledger import store_from_conn

    store = store_from_conn(conn)
    decomp = compute_account_decomposition(conn, ledger, store, config, logger)
    if decomp.get('query_failed'):
        if strict:
            if logger:
                logger.error('[启动] CTP 查询失败，无法校验外部仓 JSON')
            return False
        return True

    live = normalize_inst_map(decomp.get('external') or {})
    if decomp.get('balanced'):
        live = {}

    if stored is None:
        if live and strict:
            if logger:
                logger.error(
                    '[启动] 当前账户分解存在外部差额，但未找到或已过期 '
                    f'{path}；请人工冷启动确认，或删除 ack 后重确认'
                )
            return False
        return True

    if not external_maps_match(stored, live):
        if strict:
            if logger:
                logger.error(
                    '[启动] 外部仓 JSON 与当前 CTP 分解不一致；'
                    f'文件={stored} 当前={live}；'
                    '请人工冷启动重确认，或删除 external_positions_ack.json'
                )
            return False
        if logger:
            logger.warning(
                '[启动] 外部仓 JSON 与当前分解不一致，已忽略 JSON（非 strict）'
            )
        return True

    register_acknowledged_external(config, stored, conn, persist=False)
    if logger:
        if stored:
            logger.info(
                f'[启动] 已从 JSON 校验并恢复外部仓登记 ({len(stored)} 个合约)'
            )
        elif has_file:
            logger.info('[启动] 外部仓 JSON 与当前分解一致（无外部差额）')
    return True


def external_explains_ctp_ahead(
    inst: str, ctp_vol: int, book_vol: int, config: dict,
) -> bool:
    """
    True when user acknowledged external gap and CTP-ahead equals that gap.
    Only applies to CTP ahead (not CSV ahead).
    """
    if not config.get('_external_positions_acknowledged'):
        return False
    ext = int(get_acknowledged_external(config).get(
        str(inst).strip().upper(), 0,
    ))
    if ext <= 0:
        return False
    if ctp_vol <= book_vol and not (ctp_vol != 0 and book_vol == 0):
        return False
    return (int(ctp_vol) - int(book_vol)) == ext


def external_explains_strangle_gap(
    inst: str, gap: int, config: dict,
) -> bool:
    if gap <= 0:
        return False
    if not config.get('_external_positions_acknowledged'):
        return False
    ext = int(get_acknowledged_external(config).get(
        str(inst).strip().upper(), 0,
    ))
    return ext > 0 and ext == int(gap)


def _spread_keys(spread_tradeinfo: list) -> Set[Tuple[str, str]]:
    out: Set[Tuple[str, str]] = set()
    for item in spread_tradeinfo or []:
        sym = (item.get('future') or '').lower()
        month = str(item.get('month') or '').strip()
        if sym and month:
            out.add((sym, month))
    return out


def _strangle_symbols(strangle_tradeinfo: list) -> Set[str]:
    return {
        (item.get('future') or '').lower()
        for item in (strangle_tradeinfo or [])
        if item.get('future')
    }


def _inst_in_universe(
    inst: str,
    conn,
    spread_keys: Set[Tuple[str, str]],
    strangle_syms: Set[str],
) -> bool:
    from auto_connection import extract_symbol_prefix
    from auto_connection_utils import months_match

    sym = extract_symbol_prefix(inst).lower()
    if not sym:
        return False
    if sym in strangle_syms:
        return True
    for trade_sym, month in spread_keys:
        if trade_sym != sym:
            continue
        try:
            norm = conn._normalize_month(trade_sym, month) if conn else month
        except Exception:
            norm = month
        if months_match(inst, month, norm):
            return True
    return False


def compute_account_decomposition(
    conn,
    ledger,
    store,
    config: dict,
    logger=None,
) -> dict:
    """
    For tradeinfo-covered instruments:

        CTP_signed = spread_signed + strangle_long + external

    Returns dict with keys:
        balanced, query_failed, external, issues, summary_lines
    """
    ctp_raw = query_ctp_signed_positions(conn, logger)
    if ctp_raw is None:
        return {
            'balanced': False,
            'query_failed': True,
            'external': {},
            'issues': ['CTP 持仓查询失败'],
            'summary_lines': ['【账户分解】CTP 持仓查询失败'],
        }

    spread_info = config.get('spread_tradeinfo') or []
    strangle_info = config.get('strangle_tradeinfo') or []
    spread_keys = _spread_keys(spread_info)
    strangle_syms = _strangle_symbols(strangle_info)

    ctp = normalize_inst_map(ctp_raw)
    spread = normalize_inst_map(store.list_leg_claims() if store else {})
    strangle = normalize_inst_map(merge_strangle_owned_volumes(ledger))

    instruments: Set[str] = set()
    for inst in list(spread) + list(strangle) + list(ctp):
        if _inst_in_universe(inst, conn, spread_keys, strangle_syms):
            instruments.add(inst.upper())

    external: Dict[str, int] = {}
    issues: List[str] = []
    detail_lines: List[str] = []

    for inst in sorted(instruments):
        c = int(ctp.get(inst, 0))
        sp = int(spread.get(inst, 0))
        st = int(strangle.get(inst, 0))
        ext = c - sp - st
        if ext == 0:
            if c != 0 or sp != 0 or st != 0:
                detail_lines.append(
                    f'  {inst}: CTP={c} 价差={sp} 宽跨={st} ✓'
                )
            continue
        external[inst] = ext
        msg = f'{inst}: CTP={c} 价差={sp} 宽跨={st} 外部={ext:+d}'
        issues.append(msg)
        detail_lines.append(f'  {msg}')

    balanced = len(external) == 0
    lines = ['【账户分解】CTP = 价差 + 宽跨 + 外部（tradeinfo 范围）']
    if balanced:
        if detail_lines:
            lines.extend(detail_lines)
        else:
            lines.append('  (tradeinfo 范围内无持仓)')
        lines.append('  结论: 一致，无外部差额')
    else:
        lines.append(
            f'  共 {len(external)} 个合约存在外部差额（可能为其他策略/手工仓）'
        )
        lines.extend(detail_lines[:20])
        if len(detail_lines) > 20:
            lines.append(f'  ... 共 {len(detail_lines)} 条')

    if logger:
        for line in lines:
            logger.info(line)

    return {
        'balanced': balanced,
        'query_failed': False,
        'external': external,
        'issues': issues,
        'summary_lines': lines,
    }


def format_account_decomposition_summary(result: dict) -> str:
    return '\n'.join(result.get('summary_lines') or [])
