"""Derive spread leg claims as CTP signed position minus strangle ownership."""

from __future__ import annotations

from typing import Dict, Optional, Tuple

from import_spread_positions import (
    save_spread_positions_csv,
    spread_positions_csv_path,
    sync_spread_leg_claims,
)
from spread_position_adjust import merge_strangle_owned_volumes


def _position_volume(pos: dict) -> int:
    for key in ('position', 'Position', 'volume', 'Volume'):
        val = pos.get(key)
        if val is not None:
            try:
                return int(val)
            except (TypeError, ValueError):
                continue
    return 0


def _is_long(direction) -> bool:
    return direction in ('2', 2, 'LONG')


def _is_short(direction) -> bool:
    return direction in ('3', 3, 'SHORT')


def query_ctp_signed_positions(conn, logger=None) -> Optional[Dict[str, int]]:
    """
    CTP net per instrument: long positive, short negative.
    Returns None if query failed.
    """
    try:
        positions = conn.query_positions_sync(timeout=10, use_cache=False)
    except TypeError:
        positions = conn.query_positions_sync(timeout=10)
    except Exception as e:
        if logger:
            logger.warning(f'[价差推导] CTP 持仓查询失败: {e}')
        return None

    if positions is None:
        if logger:
            logger.warning('[价差推导] CTP 持仓查询返回 None')
        return None

    signed: Dict[str, int] = {}
    for pos in positions:
        vol = _position_volume(pos)
        if vol <= 0:
            continue
        inst = (pos.get('instrument') or pos.get('InstrumentID') or '').strip()
        if not inst:
            continue
        direction = pos.get('direction') or pos.get('PosiDirection') or ''
        if _is_long(direction):
            signed[inst] = signed.get(inst, 0) + vol
        elif _is_short(direction):
            signed[inst] = signed.get(inst, 0) - vol
    return signed


def derive_spread_claims_from_ctp(
    conn,
    ledger,
    logger=None,
    config: dict = None,
) -> Tuple[Optional[Dict[str, int]], str]:
    """
    spread_claims[inst] = CTP_signed[inst] - strangle_owned[inst]

    Only instruments present in CTP with non-zero net are considered.
    strangle_owned includes leg_claims and unmatched long legs.
    When config/spread_tradeinfo is set, skip instruments outside it.
    """
    ctp_signed = query_ctp_signed_positions(conn, logger)
    if ctp_signed is None:
        return None, 'CTP 持仓查询失败'

    cfg = config or getattr(conn, 'config', None) or {}
    spread_info = cfg.get('spread_tradeinfo') or []
    filter_tradeinfo = bool(
        (cfg.get('dual_strategy') or {}).get(
            'spread_derive_require_tradeinfo_match', True,
        )
    )

    strangle_owned = merge_strangle_owned_volumes(ledger)
    strangle_by_upper = {
        str(k).strip().upper(): int(v)
        for k, v in strangle_owned.items()
        if int(v) > 0
    }

    spread: Dict[str, int] = {}
    warnings = []
    for inst, net in ctp_signed.items():
        if filter_tradeinfo and spread_info:
            from spread_claims_guard import instrument_in_spread_tradeinfo
            if not instrument_in_spread_tradeinfo(inst, conn, spread_info):
                if logger:
                    logger.info(
                        f'[价差推导] 跳过 {inst}：不在 spread tradeinfo'
                    )
                continue
        strangle_vol = strangle_by_upper.get(inst.upper(), 0)
        rem = int(net) - strangle_vol
        if rem != 0:
            spread[inst] = rem
        if strangle_vol > max(net, 0) and net >= 0:
            warnings.append(
                f'{inst}: 宽跨认领 {strangle_vol} > CTP多头 {net}，推导价差={rem}'
            )

    for inst, sv in strangle_by_upper.items():
        if sv > 0 and not any(k.upper() == inst for k in ctp_signed):
            warnings.append(f'{inst}: 宽跨认领 {sv} 手但 CTP 无该合约持仓')

    if logger:
        logger.info(
            f'[价差推导] CTP {len(ctp_signed)} 个合约, 宽跨认领 {len(strangle_by_upper)} 个, '
            f'价差认领 {len(spread)} 个'
        )
        for msg in warnings[:10]:
            logger.warning(f'[价差推导] {msg}')

    note = '; '.join(warnings[:3]) if warnings else ''
    return spread, note


def apply_derived_spread_from_ctp(conn, ledger, store, config, logger=None) -> Optional[Dict[str, int]]:
    """Persist spread claims derived from CTP minus strangle into CSV and runtime store."""
    import time as _time

    claims, _note = derive_spread_claims_from_ctp(
        conn, ledger, logger, config=config,
    )
    if claims is None:
        return None

    path = spread_positions_csv_path(config)
    save_spread_positions_csv(path, claims)
    if store is not None:
        sync_spread_leg_claims(store, config, logger=logger)
    elif logger:
        logger.warning('[启动] 价差 store 未挂载，仅写入 CSV')

    if logger:
        logger.info(f'[启动] 价差认领已更新 (CTP−宽跨) -> {path}')
        if claims:
            for inst, vol in sorted(claims.items()):
                side = '多' if vol > 0 else '空'
                logger.info(f'  {inst} {side} x{abs(vol)}')
        else:
            logger.info('  (无价差认领持仓)')

    config['_spread_derived_at_startup'] = True

    # B12: derive 刚写入 CSV / store，OnRtnTrade 等异步路径还可能让账本和 CTP
    # 短暂错开。给后续 reconcile 一段豁免窗口，把 halt 降级为仅记录 issues，
    # 避免"刚 derive 又被立即锁成 close-only"的体感。
    runtime = getattr(conn, '_runtime_state', None)
    if runtime is not None:
        dual = (config.get('dual_strategy') or {}) if config else {}
        grace = float(dual.get('reconcile_grace_after_derive_sec', 90))
        if grace > 0:
            runtime['_reconcile_grace_until'] = _time.time() + grace
            if logger:
                logger.info(
                    f'[启动] 已开启对账豁免窗口 {grace:.0f}s '
                    '(期间差异仅记录，不强制 halt)'
                )
    return claims
