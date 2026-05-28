"""Validate spread leg claims against tradeinfo and CTP (audit + fill gate)."""

from __future__ import annotations

import json
import os
import re
from typing import Dict, List, Optional, Set, Tuple

_SYMBOL_PREFIX_RE = re.compile(r'^([A-Za-z]+)')


def _symbol_prefix(instrument: str) -> str:
    if not instrument:
        return ''
    m = _SYMBOL_PREFIX_RE.match(instrument.strip())
    return m.group(1).lower() if m else ''


def _spread_keys(spread_tradeinfo: list) -> Set[Tuple[str, str]]:
    out: Set[Tuple[str, str]] = set()
    for item in spread_tradeinfo or []:
        sym = (item.get('future') or '').lower()
        month = str(item.get('month') or '').strip()
        if sym and month:
            out.add((sym, month))
    return out


def instrument_in_spread_tradeinfo(
    instrument: str,
    conn,
    spread_tradeinfo: list,
) -> bool:
    """True when contract symbol+month matches any spread tradeinfo row."""
    inst = (instrument or '').strip()
    if not inst:
        return False
    keys = _spread_keys(spread_tradeinfo)
    if not keys:
        return True

    sym = _symbol_prefix(inst)
    if not sym:
        return False

    try:
        from auto_connection_utils import months_match
    except Exception:
        months_match = None  # type: ignore

    for trade_sym, month in keys:
        if trade_sym != sym:
            continue
        if months_match is None:
            return True
        try:
            norm = conn._normalize_month(trade_sym, month) if conn else month
        except Exception:
            norm = month
        if months_match(inst, month, norm):
            return True
    return False


def invalid_spread_claim_keys(
    claims: Dict[str, int],
    spread_tradeinfo: list,
    conn=None,
    ctp_signed: Optional[Dict[str, int]] = None,
) -> Set[str]:
    """Instrument keys in claims that fail tradeinfo or CTP orphan checks."""
    bad: Set[str] = set()
    keys = _spread_keys(spread_tradeinfo)

    for inst, vol in (claims or {}).items():
        v = int(vol)
        if v == 0:
            continue
        sym = _symbol_prefix(inst)
        if keys and not any(s == sym for s, _m in keys):
            bad.add(inst)
            continue
        if keys and conn is not None:
            if not instrument_in_spread_tradeinfo(inst, conn, spread_tradeinfo):
                bad.add(inst)
                continue
        if ctp_signed is not None:
            ctp_vol = int(ctp_signed.get(inst, 0))
            if ctp_vol == 0 and abs(v) > 0:
                bad.add(inst)
    return bad


def audit_spread_claims(
    claims: Dict[str, int],
    spread_tradeinfo: list,
    conn=None,
    ctp_signed: Optional[Dict[str, int]] = None,
) -> List[str]:
    """Return human-readable issues (empty = clean)."""
    issues: List[str] = []
    keys = _spread_keys(spread_tradeinfo)

    for inst, vol in sorted((claims or {}).items()):
        v = int(vol)
        if v == 0:
            continue
        sym = _symbol_prefix(inst)
        if keys and not any(s == sym for s, _m in keys):
            issues.append(
                f'{inst}: CSV={v} 品种 {sym.upper()} 不在 spread tradeinfo（'
                '疑似宽跨/错月/测试合约误入价差认领）'
            )
            continue
        if keys and conn is not None:
            if not instrument_in_spread_tradeinfo(inst, conn, spread_tradeinfo):
                issues.append(
                    f'{inst}: CSV={v} 合约月份与 spread tradeinfo 不匹配'
                )
        if ctp_signed is not None:
            ctp_vol = int(ctp_signed.get(inst, 0))
            if ctp_vol == 0 and abs(v) > 0:
                issues.append(
                    f'{inst}: CSV={v} 但 CTP 无该合约净持仓（认领孤儿）'
                )
    return issues


def purge_invalid_spread_claims(
    config: dict,
    conn,
    spread_tradeinfo: list,
    store=None,
    logger=None,
) -> int:
    """Drop CSV rows outside spread tradeinfo or with zero CTP net (orphans).

    Only runs when CTP signed positions are available; returns removed count.
    """
    from import_spread_positions import (
        load_spread_positions_csv,
        save_spread_positions_csv,
        spread_positions_csv_path,
    )

    dual = config.get('dual_strategy') or {}
    if not dual.get('spread_purge_invalid_claims_on_startup', True):
        return 0

    path = spread_positions_csv_path(config)
    if not os.path.isfile(path):
        return 0

    try:
        claims = load_spread_positions_csv(path)
    except Exception as e:
        if logger:
            logger.warning(f'[价差持仓] 净化跳过：读取 CSV 失败: {e}')
        return 0
    if not claims:
        return 0

    try:
        from spread_derive import query_ctp_signed_positions
        ctp_signed = query_ctp_signed_positions(conn, logger)
    except Exception as e:
        if logger:
            logger.warning(f'[价差持仓] 净化跳过：CTP 持仓查询失败: {e}')
        return 0
    if ctp_signed is None:
        if logger:
            logger.warning('[价差持仓] 净化跳过：CTP 持仓不可用')
        return 0

    bad = invalid_spread_claim_keys(
        claims, spread_tradeinfo, conn=conn, ctp_signed=ctp_signed,
    )
    if not bad:
        return 0

    removed = 0
    for inst in bad:
        vol = claims.pop(inst, None)
        if vol is not None:
            removed += 1
            if logger:
                logger.warning(
                    f'[价差持仓] 已移除无效认领 {inst} CSV={vol} '
                    '(非 spread tradeinfo 或 CTP 无仓；非价差成交入账)'
                )

    save_spread_positions_csv(path, claims)
    if store is not None:
        store.set_leg_claims(claims)
    if logger:
        logger.info(
            f'[价差持仓] CSV 已净化：移除 {removed} 条无效认领 -> {path}'
        )
    return removed


def repair_spread_trade_journals(
    config: dict,
    conn=None,
    logger=None,
) -> Tuple[int, int]:
    """Drop journal lines whose instrument is outside spread tradeinfo.

    Returns (removed_count, kept_count). Rewrites each shard in place.
    """
    from spread_fill_sync import _journal_path
    from trade_journal import _journal_glob_paths

    spread_info = config.get('spread_tradeinfo') or []
    if not spread_info:
        return 0, 0

    journal_base = _journal_path(config)
    paths = _journal_glob_paths(journal_base, config)
    if not paths:
        return 0, 0

    removed = 0
    kept = 0
    for path in paths:
        if not os.path.isfile(path):
            continue
        out_lines: List[str] = []
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                raw = line.strip()
                if not raw:
                    continue
                try:
                    row = json.loads(raw)
                except Exception:
                    out_lines.append(line if line.endswith('\n') else line + '\n')
                    kept += 1
                    continue
                inst = (row.get('instrument') or '').strip()
                if row.get('skipped') == 'not_in_spread_tradeinfo':
                    removed += 1
                    continue
                if inst and not instrument_in_spread_tradeinfo(inst, conn, spread_info):
                    removed += 1
                    if logger:
                        logger.info(
                            f'[价差 journal] 移除无效行 {path}: '
                            f'{inst} OrderRef={row.get("order_ref")}'
                        )
                    continue
                out_lines.append(line if line.endswith('\n') else line + '\n')
                kept += 1
        from atomic_io import atomic_write_text
        atomic_write_text(path, ''.join(out_lines))
    return removed, kept


def format_spread_claims_audit(issues: List[str]) -> str:
    if not issues:
        return ''
    lines = ['【价差认领审计】']
    for msg in issues[:20]:
        lines.append(f'  ⚠ {msg}')
    if len(issues) > 20:
        lines.append(f'  ... 共 {len(issues)} 条')
    lines.append(
        '  修复：启动确认选「CTP−宽跨推导」重写 spread_positions.csv，'
        '或手工编辑 CSV/ledger 后运行 scripts/invalidate_startup_ack.py 重确认'
    )
    return '\n'.join(lines)
