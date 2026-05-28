"""逐品种策略状态快照（飞书「持仓查询」）。

状态三态（当前满足的主状态）：
  - 开仓：本轮满足开仓/再平衡/建仓条件
  - 平仓：本轮满足平仓扫描/执行条件
  - 其他：既不满足平仓也不满足开仓（含不可运行、等待、门闸禁开等）
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

STATUS_OPEN = "开仓"
STATUS_CLOSE = "平仓"
STATUS_OTHER = "其他"


def _cond(name: str, ok: bool, detail: str) -> Dict[str, Any]:
    return {"name": name, "ok": bool(ok), "detail": detail}


def _sum_current(rows: List[Dict[str, Any]]) -> int:
    total = 0
    for row in rows:
        try:
            total += int(row.get("current_groups") or 0)
        except Exception:
            continue
    return total


def _sum_target(rows: List[Dict[str, Any]]) -> int:
    total = 0
    for row in rows:
        try:
            total += int(row.get("target_groups") or 0)
        except Exception:
            continue
    return total


def _format_gates(conds: List[Dict[str, Any]]) -> str:
    failed = [c for c in conds if not c.get("ok")]
    if not failed:
        return "全通过"
    return "; ".join(f"{c.get('name')}❌" for c in failed)


def _query_positions(conn) -> Tuple[Optional[List[dict]], str]:
    err = ""
    positions = None
    for kwargs in (
        {"timeout": 10, "use_cache": False},
        {"timeout": 10},
        {},
    ):
        try:
            positions = conn.query_positions_sync(**kwargs)
            if positions is not None:
                return positions, ""
        except TypeError:
            if not kwargs:
                continue
            try:
                positions = conn.query_positions_sync()
            except Exception as e:
                err = str(e)
        except Exception as e:
            err = str(e)
    return positions, err


def _get_round_vix(conn, vix_engine, sym: str, logger) -> Optional[float]:
    cache = (getattr(conn, "_runtime_state", None) or {}).get("_spread_round_vix_cache")
    if isinstance(cache, dict) and sym.lower() in cache:
        try:
            return float(cache[sym.lower()])
        except (TypeError, ValueError):
            pass
    if vix_engine is None:
        return None
    try:
        return vix_engine.calculate_vix(sym.lower(), conn, logger)
    except Exception:
        return None


def _spread_runtime_block(
    conn,
    symbol: str,
    config: dict,
    feishu_paused: bool,
) -> Tuple[bool, str]:
    sym = (symbol or "").lower()
    if feishu_paused:
        return True, "飞书全停(含平仓扫描)"
    try:
        from auto_processor import is_trading_time  # type: ignore

        profile = config.get("_runtime_profile") or {}
        enforce = profile.get(
            "enforce_trading_hours",
            not config.get("is_simulation"),
        )
        if enforce and not is_trading_time(symbol):
            return True, "非交易时段"
    except Exception:
        pass
    if getattr(conn, "_reconnect_quarantine", False):
        return True, "CTP重连隔离"
    if not getattr(conn, "td_logined", True) or not getattr(conn, "md_logined", True):
        return True, "交易/行情未登录"
    runtime = getattr(conn, "_runtime_state", None) or {}
    cb = runtime.get("_circuit_breaker")
    if cb:
        try:
            suspended, remaining = cb.is_global_suspended()
            if suspended:
                return True, f"全局断路器(剩{int(remaining)}s)"
            suspended, remaining = cb.is_suspended(symbol)
            if suspended:
                return True, f"品种断路器(剩{int(remaining)}s)"
        except Exception:
            pass
    return False, ""


def _spread_gate_conds(
    spread_halt: bool,
    margin_halt_open: bool,
    journal_halt_open: bool,
    spread_filled: int,
    spread_daily_limit: int,
    feishu_paused: bool,
) -> List[Dict[str, Any]]:
    return [
        _cond("reconcile_halt", not spread_halt, "对账一致" if not spread_halt else "对账halt"),
        _cond("margin_halt", not margin_halt_open, "保证金未超限" if not margin_halt_open else "保证金超限"),
        _cond("journal_halt", not journal_halt_open, "journal正常" if not journal_halt_open else "journal未完成入账"),
        _cond(
            "daily_limit",
            spread_filled < spread_daily_limit,
            f"日限 {spread_filled}/{spread_daily_limit}",
        ),
        _cond("feishu_pause", not feishu_paused, "未暂停" if not feishu_paused else "飞书暂停"),
    ]


def _resolve_spread_status(
    conn,
    item: dict,
    config: dict,
    positions: Optional[List[dict]],
    positions_err: str,
    vix_engine,
    spread_halt: bool,
    margin_halt_open: bool,
    journal_halt_open: bool,
    journal_halt_reason: str,
    spread_open_ok: bool,
    spread_filled: int,
    spread_daily_limit: int,
    feishu_paused: bool,
    logger,
) -> Tuple[str, str]:
    symbol = item.get("future") or ""
    month = item.get("month") or ""
    sym = symbol.lower()
    vol_basis = float(item.get("vol_basis") or 0)
    vol_of_combo = int(item.get("vol_of_combo") or 1)

    blocked, block_reason = _spread_runtime_block(conn, symbol, config, feishu_paused)
    if blocked:
        return STATUS_OTHER, block_reason

    if positions is None:
        return STATUS_OTHER, f"持仓查询失败{(':' + positions_err) if positions_err else ''}"

    future_price = float((getattr(conn, "future_prices", None) or {}).get(sym) or 0.0)
    vix = _get_round_vix(conn, vix_engine, sym, logger)

    # --- 价差账本持仓（统一用于平仓与开仓/再平衡分析） ---
    symbol_positions: List[dict] = []
    try:
        from spread_close_ledger import build_positions_from_spread_claims  # type: ignore
        from spread_ledger import store_from_conn  # type: ignore

        store = store_from_conn(conn)
        if store is not None:
            symbol_positions = build_positions_from_spread_claims(
                store, conn, symbol, month,
            )
    except Exception:
        symbol_positions = []

    if symbol_positions and vix is not None:
        try:
            from auto_closer_conditions import (  # type: ignore
                check_close_conditions_with_urgency,
            )

            urgency, close_reason = check_close_conditions_with_urgency(
                conn,
                vix,
                vol_basis,
                future_price,
                symbol_positions,
                symbol,
                month,
                config,
                logger,
            )
            if urgency:
                tag = "紧急" if urgency == "urgent" else "常规"
                return STATUS_CLOSE, f"{tag}平仓: {close_reason}"
        except Exception as e:
            pass

    if symbol_positions and vix is None:
        # 有仓但 VIX 不可算：仍可能价格触发平仓，此处保守标其他
        pass

    # --- 开仓/再平衡条件（对齐 process_symbol 开仓段） ---
    gates_ok = (
        spread_open_ok
        and not spread_halt
        and not margin_halt_open
        and not journal_halt_open
    )
    if not gates_ok:
        parts = []
        if spread_halt:
            parts.append("对账halt禁开")
        if margin_halt_open:
            parts.append("保证金halt禁开")
        if journal_halt_open:
            parts.append(
                f"journal_halt禁开{(':' + journal_halt_reason) if journal_halt_reason else ''}",
            )
        if not spread_open_ok:
            parts.append("日限/查询禁开")
        if spread_filled >= spread_daily_limit:
            parts.append(f"日笔数达限({spread_filled}/{spread_daily_limit})")
        close_hint = "无平仓条件" if not symbol_positions else "本轮无平仓触发"
        return STATUS_OTHER, f"{'; '.join(parts)}; {close_hint}"

    if vix is None:
        return STATUS_OTHER, "VIX无法计算(已扫平仓)"

    open_threshold = (
        config.get("VIX_TRIGGER_MULTIPLIER", 1.0) * vol_basis * 100
    )
    if vix <= open_threshold:
        vix_exit = vol_basis * 100 * config.get("VIX_EXIT_MULTIPLIER", 1.0)
        return STATUS_OTHER, (
            f"VIX未达开仓({vix:.2f}/{open_threshold:.2f}); "
            f"未达平仓({vix:.2f}/{vix_exit:.2f})"
        )

    min_days = config.get("min_days_to_expiry", 10)
    dte = (
        conn.get_days_to_expiry(symbol, month)
        if hasattr(conn, "get_days_to_expiry") else None
    )
    if dte is None:
        return STATUS_OTHER, "无法获取DTE"
    if dte < min_days:
        return STATUS_OTHER, f"DTE={dte}<{min_days}天"

    if future_price <= 0:
        return STATUS_OTHER, "期货价格无效"

    closing_cfg = config.get("closing", {}) or {}
    cooldown_sec = int(closing_cfg.get("cooldown_minutes", 30) or 0) * 60
    if cooldown_sec > 0:
        key = f"_close_cooldown_until_{sym}"
        until = float((conn._runtime_state or {}).get(key) or 0)
        if time.time() < until:
            rem = int(until - time.time())
            return STATUS_OTHER, f"平仓冷却中(剩{rem}s)"

    analysis_positions = symbol_positions if symbol_positions else positions
    try:
        from auto_position import analyze_position_imbalance  # type: ignore

        analysis = analyze_position_imbalance(
            conn,
            analysis_positions,
            symbol,
            month,
            vol_of_combo,
            config,
            future_price,
            logger,
        )
    except Exception as e:
        return STATUS_OTHER, f"持仓分析失败: {e}"

    if analysis.get("is_imbalanced"):
        needed_b = int(analysis.get("needed_B_volume") or 0)
        b_rem = int(analysis.get("B_limit") or 0) - (
            int(analysis.get("B_total") or 0) + int(analysis.get("B_pending") or 0)
        )
        if needed_b > 0 and b_rem >= needed_b:
            return STATUS_OPEN, f"VIX触发; 需补B {needed_b}手"
        return STATUS_OTHER, f"持仓不平衡需补B{needed_b}手但额度不足"

    if analysis.get("needs_A_supplement"):
        needed_a = int(analysis.get("needed_A_volume") or 0)
        a_rem = int(analysis.get("A_limit") or 0) - (
            int(analysis.get("A_total") or 0) + int(analysis.get("A_pending") or 0)
        )
        if needed_a > 0 and a_rem >= needed_a:
            return STATUS_OPEN, f"VIX触发; 需补A {needed_a}手"
        return STATUS_OTHER, f"需补A{needed_a}手但额度不足"

    a_eff = int(analysis.get("A_total") or 0) + int(analysis.get("A_pending") or 0)
    b_eff = int(analysis.get("B_total") or 0) + int(analysis.get("B_pending") or 0)
    a_lim = int(analysis.get("A_limit") or 0)
    b_lim = int(analysis.get("B_limit") or 0)
    max_new = min(a_lim - a_eff, (b_lim - b_eff) // 2, vol_of_combo)
    if max_new >= 1:
        return STATUS_OPEN, (
            f"VIX触发({vix:.2f}>{open_threshold:.2f}); 可开至多{max_new}组"
        )

    return STATUS_OTHER, "额度已满/无新组空间; 无平仓触发"


def _strangle_runtime_block(
    conn,
    symbol: str,
    config: dict,
    feishu_paused: bool,
) -> Tuple[bool, str]:
    return _spread_runtime_block(conn, symbol, config, feishu_paused)


def _resolve_strangle_status(
    conn,
    item: dict,
    ledger,
    config: dict,
    vix_engine,
    strangle_reconcile_halt: bool,
    margin_halt_open: bool,
    journal_halt_open: bool,
    journal_halt_reason: str,
    strangle_buy_spent: float,
    strangle_buy_limit: float,
    feishu_paused: bool,
    logger,
    groups_map: Dict[tuple, int],
    um_open: Dict[tuple, int],
    um_close: Dict[tuple, int],
) -> Tuple[str, str]:
    symbol = (item.get("future") or "").upper()
    month = str(item.get("month") or "")
    sym = symbol.lower()
    key = (symbol, month)
    vol_of_combo = int(item.get("vol_of_combo") or 0)

    blocked, block_reason = _strangle_runtime_block(
        conn, symbol, config, feishu_paused,
    )
    if blocked:
        return STATUS_OTHER, block_reason

    # closing 重试
    try:
        for pos in ledger.list_positions(symbol, month):
            if pos.get("status") == "closing":
                return STATUS_CLOSE, f"持仓平仓中 id={str(pos.get('id', ''))[:8]}"
    except Exception:
        pass

    # open 持仓平仓信号
    try:
        from straggle_signals import check_exit  # type: ignore

        for pos in ledger.list_positions(symbol, month):
            if pos.get("status") != "open":
                continue
            should_close, reason, _extra = check_exit(
                conn, pos, vix_engine, config, logger,
            )
            if should_close:
                return STATUS_CLOSE, reason
    except Exception:
        pass

    close_um = int(um_close.get(key, 0))
    if close_um > 0:
        return STATUS_CLOSE, f"平仓类未配对腿 {close_um} 条待处理"

    # 开仓
    try:
        from straggle_risk import can_open_new_group  # type: ignore
        from straggle_signals import check_entry  # type: ignore

        if journal_halt_open:
            if journal_halt_reason:
                return STATUS_OTHER, f"journal_halt禁开: {journal_halt_reason}"
            return STATUS_OTHER, "journal_halt禁开"

        ok_risk, risk_reason = can_open_new_group(
            ledger, symbol, month, vol_of_combo, config,
        )
        if not ok_risk:
            open_um = int(um_open.get(key, 0))
            extra = f"; 未配对open={open_um}" if open_um else ""
            return STATUS_OTHER, f"{risk_reason}{extra}"

        entry = check_entry(conn, item, vix_engine, config, logger)
        if entry.get("ok"):
            cur = int(groups_map.get(key, 0))
            return STATUS_OPEN, (
                f"{entry.get('reason', '建仓条件满足')}; 当前{cur}/{vol_of_combo}组"
            )
        return STATUS_OTHER, entry.get("reason") or "不满足建仓条件"
    except Exception as e:
        return STATUS_OTHER, f"状态评估失败: {e}"


def build_spread_symbol_rows(
    conn,
    spread_tradeinfo: List[dict],
    config: dict,
    vix_engine,
    spread_halt: bool,
    margin_halt_open: bool,
    journal_halt_open: bool,
    journal_halt_reason: str,
    spread_open_ok: bool,
    spread_filled: int,
    spread_daily_limit: int,
    feishu_paused: bool,
    logger,
) -> List[dict]:
    rows: List[dict] = []
    positions, positions_err = _query_positions(conn)
    gate_conds_template = _spread_gate_conds(
        spread_halt,
        margin_halt_open,
        journal_halt_open,
        spread_filled,
        spread_daily_limit,
        feishu_paused,
    )

    for item in spread_tradeinfo:
        sym = item.get("future")
        month = item.get("month")
        target_groups = int(item.get("vol_of_combo") or 0)
        current_groups = None
        a_total = b_total = a_limit = b_limit = None
        pos_detail = ""

        ledger_positions = []
        try:
            from spread_close_ledger import build_positions_from_spread_claims  # type: ignore
            from spread_ledger import store_from_conn  # type: ignore

            store = store_from_conn(conn)
            if store is not None:
                ledger_positions = build_positions_from_spread_claims(
                    store, conn, sym, month,
                )
        except Exception:
            ledger_positions = []

        if positions is not None:
            try:
                from auto_position import analyze_position_imbalance  # type: ignore

                sym_lower = (sym or "").lower()
                fp = float((getattr(conn, "future_prices", None) or {}).get(sym_lower) or 0.0)
                analysis_positions = ledger_positions if ledger_positions else positions
                analysis = analyze_position_imbalance(
                    conn,
                    analysis_positions,
                    sym,
                    month,
                    target_groups or 1,
                    config,
                    fp,
                    logger,
                )
                a_total = int(analysis.get("A_total") or 0)
                b_total = int(analysis.get("B_total") or 0)
                a_limit = int(analysis.get("A_limit") or 0)
                b_limit = int(analysis.get("B_limit") or 0)
                current_groups = min(a_total, b_total // 2)
                pos_detail = f"A={a_total}/{a_limit}, B={b_total}/{b_limit}"
            except Exception as e:
                pos_detail = f"持仓换算失败: {e}"
        else:
            pos_detail = f"持仓查询失败{(':' + positions_err) if positions_err else ''}"

        status, reason = _resolve_spread_status(
            conn,
            item,
            config,
            positions,
            positions_err,
            vix_engine,
            spread_halt,
            margin_halt_open,
            journal_halt_open,
            journal_halt_reason,
            spread_open_ok,
            spread_filled,
            spread_daily_limit,
            feishu_paused,
            logger,
        )

        rows.append({
            "symbol": sym,
            "month": month,
            "current_groups": current_groups,
            "target_groups": target_groups,
            "status": status,
            "status_reason": reason,
            "gates": gate_conds_template,
            "meta": {
                "position_detail": pos_detail,
                "A_total": a_total,
                "B_total": b_total,
                "A_limit": a_limit,
                "B_limit": b_limit,
            },
        })

    return rows


def build_strangle_symbol_rows(
    conn,
    ledger,
    strangle_tradeinfo: List[dict],
    config: dict,
    vix_engine,
    strangle_reconcile_halt: bool,
    margin_halt_open: bool,
    journal_halt_open: bool,
    journal_halt_reason: str,
    strangle_buy_spent: float,
    strangle_buy_limit: float,
    feishu_paused: bool,
    logger,
) -> List[dict]:
    try:
        from strangle_rebalance_close_only import CLOSE_KINDS  # type: ignore
    except Exception:
        CLOSE_KINDS = {"close_chp_pending"}

    rows: List[dict] = []
    try:
        positions = ledger.list_positions() or []
    except Exception:
        positions = []
    try:
        unmatched = ledger.list_unmatched_legs() or []
    except Exception:
        unmatched = []

    groups_map: Dict[tuple, int] = {}
    for p in positions:
        k = ((p.get("symbol") or "").upper(), str(p.get("month") or ""))
        groups_map[k] = groups_map.get(k, 0) + int(p.get("groups", 1) or 1)

    um_open: Dict[tuple, int] = {}
    um_close: Dict[tuple, int] = {}
    for u in unmatched:
        k = ((u.get("symbol") or "").upper(), str(u.get("month") or ""))
        kind = str(u.get("kind") or "")
        if kind in CLOSE_KINDS:
            um_close[k] = um_close.get(k, 0) + 1
        else:
            um_open[k] = um_open.get(k, 0) + 1

    try:
        open_halted = bool(getattr(ledger, "is_open_halted", lambda: False)())
    except Exception:
        open_halted = False

    for item in strangle_tradeinfo:
        sym = (item.get("future") or "").upper()
        month = str(item.get("month") or "")
        key = (sym, month)
        target_groups = int(item.get("vol_of_combo") or 0)
        current_groups = int(groups_map.get(key, 0))

        gates = [
            _cond("reconcile_halt", not strangle_reconcile_halt,
                  "对账一致" if not strangle_reconcile_halt else "对账halt"),
            _cond("margin_halt", not margin_halt_open,
                  "保证金未超限" if not margin_halt_open else "保证金超限"),
            _cond("journal_halt", not journal_halt_open,
                  "journal正常" if not journal_halt_open else "journal未完成入账"),
            _cond(
                "buy_limit",
                strangle_buy_spent < strangle_buy_limit,
                f"当日买入 {strangle_buy_spent:.0f}/{strangle_buy_limit:.0f}",
            ),
            _cond("open_halt", not open_halted,
                  "open_halted=false" if not open_halted else "open_halted=true"),
            _cond("feishu_pause", not feishu_paused,
                  "未暂停" if not feishu_paused else "飞书暂停"),
        ]

        status, reason = _resolve_strangle_status(
            conn,
            item,
            ledger,
            config,
            vix_engine,
            strangle_reconcile_halt,
            margin_halt_open,
            journal_halt_open,
            journal_halt_reason,
            strangle_buy_spent,
            strangle_buy_limit,
            feishu_paused,
            logger,
            groups_map,
            um_open,
            um_close,
        )

        rows.append({
            "symbol": sym,
            "month": month,
            "current_groups": current_groups,
            "target_groups": target_groups,
            "status": status,
            "status_reason": reason,
            "gates": gates,
            "meta": {
                "unmatched_open": int(um_open.get(key, 0)),
                "unmatched_close": int(um_close.get(key, 0)),
            },
        })

    return rows


def build_strategy_status_snapshot(
    conn,
    ledger,
    config: dict,
    spread_tradeinfo: List[dict],
    strangle_tradeinfo: List[dict],
    vix_engine,
    spread_halt: bool,
    strangle_reconcile_halt: bool,
    margin_halt_open: bool,
    spread_open_ok: bool,
    spread_filled: int,
    spread_daily_limit: int,
    strangle_buy_spent: float,
    strangle_buy_limit: float,
    feishu_paused: bool,
    logger,
    journal_halt_open: bool = False,
    journal_halt_reason: str = "",
) -> Dict[str, Any]:
    spread_rows = build_spread_symbol_rows(
        conn,
        spread_tradeinfo,
        config,
        vix_engine,
        spread_halt=spread_halt,
        margin_halt_open=margin_halt_open,
        journal_halt_open=journal_halt_open,
        journal_halt_reason=journal_halt_reason,
        spread_open_ok=spread_open_ok,
        spread_filled=spread_filled,
        spread_daily_limit=spread_daily_limit,
        feishu_paused=feishu_paused,
        logger=logger,
    )
    strangle_rows = build_strangle_symbol_rows(
        conn,
        ledger,
        strangle_tradeinfo,
        config,
        vix_engine,
        strangle_reconcile_halt=strangle_reconcile_halt,
        margin_halt_open=margin_halt_open,
        journal_halt_open=journal_halt_open,
        journal_halt_reason=journal_halt_reason,
        strangle_buy_spent=strangle_buy_spent,
        strangle_buy_limit=strangle_buy_limit,
        feishu_paused=feishu_paused,
        logger=logger,
    )

    return {
        "ts": time.time(),
        "summary": {
            "spread_current_groups": _sum_current(spread_rows),
            "spread_target_groups": _sum_target(spread_rows),
            "strangle_current_groups": _sum_current(strangle_rows),
            "strangle_target_groups": _sum_target(strangle_rows),
            "journal_halt_open": bool(journal_halt_open),
            "journal_halt_reason": journal_halt_reason or "",
        },
        "spread": {"by_symbol": spread_rows},
        "strangle": {"by_symbol": strangle_rows},
    }


def _format_symbol_row(r: Dict[str, Any], *, strategy: str) -> List[str]:
    sym = r.get("symbol") or ""
    month = r.get("month") or ""
    cg = r.get("current_groups")
    cg_txt = "?" if cg is None else str(cg)
    tgt = r.get("target_groups") or 0
    status = r.get("status") or STATUS_OTHER
    reason = r.get("status_reason") or ""
    gates = r.get("gates") or []
    meta = r.get("meta") or {}

    lines = [
        f"- {sym}{month}: {cg_txt}/{tgt} 组 | 状态: {status}",
        f"  原因: {reason}",
        f"  门闸: {_format_gates(gates)}",
    ]
    if strategy == "spread":
        pd = meta.get("position_detail")
        if pd:
            lines.append(f"  持仓: {pd}")
    else:
        lines.append(
            f"  未配对: open={meta.get('unmatched_open', 0)} "
            f"close={meta.get('unmatched_close', 0)}"
        )
    return lines


def format_strategy_status_message(
    snapshot: Dict[str, Any],
    command_text: str = "持仓查询",
) -> str:
    ts = snapshot.get("ts")
    summary = snapshot.get("summary") or {}
    spread_rows = (snapshot.get("spread") or {}).get("by_symbol") or []
    strangle_rows = (snapshot.get("strangle") or {}).get("by_symbol") or []

    import time as _t

    lines: List[str] = []
    lines.append("📊 策略持仓明细")
    if ts:
        try:
            lines.append(f"时间：{_t.strftime('%H:%M:%S', _t.localtime(float(ts)))}")
        except Exception:
            pass
    lines.append("")
    lines.append(
        "总览："
        f"Spread {summary.get('spread_current_groups', 0)}/"
        f"{summary.get('spread_target_groups', 0)} 组，"
        f"Strangle {summary.get('strangle_current_groups', 0)}/"
        f"{summary.get('strangle_target_groups', 0)} 组"
    )
    if summary.get("journal_halt_open"):
        reason = summary.get("journal_halt_reason") or "journal未完成入账"
        lines.append(f"journal_halt：开启（{reason}）")
    lines.append("")

    cmd_lower = (command_text or "").lower()
    want_spread = "spread" in cmd_lower or "价差" in command_text
    want_strangle = "strangle" in cmd_lower or "宽跨" in command_text
    if not want_spread and not want_strangle:
        want_spread = want_strangle = True

    if want_spread:
        lines.append("【Spread】")
        if not spread_rows:
            lines.append("- 无")
        for r in spread_rows:
            lines.extend(_format_symbol_row(r, strategy="spread"))

    if want_strangle:
        lines.append("")
        lines.append("【Strangle】")
        if not strangle_rows:
            lines.append("- 无")
        for r in strangle_rows:
            lines.extend(_format_symbol_row(r, strategy="strangle"))

    return "\n".join(lines)
