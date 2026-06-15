"""统一运行健康摘要（纯可观测性，不改变 halt / 交易语义）。

供心跳文件、每日报平安、飞书持仓查询顶栏、ops 脚本共用。
"""

from __future__ import annotations

import os
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from maintenance_mode import is_maintenance_mode


def _runtime(conn) -> dict:
    if conn is None:
        return {}
    state = getattr(conn, '_runtime_state', None)
    return state if isinstance(state, dict) else {}


def _flag(value) -> str:
    return '是' if value else '否'


def _fmt_ts(ts: Optional[float]) -> str:
    if not ts:
        return '未知'
    try:
        return datetime.fromtimestamp(float(ts)).isoformat(timespec='seconds')
    except (TypeError, ValueError, OSError):
        return '未知'


def _resolve_feishu_paused(feishu_paused: Optional[bool]) -> bool:
    if feishu_paused is not None:
        return bool(feishu_paused)
    try:
        from auto_feishu_command import is_trading_paused
        return bool(is_trading_paused())
    except Exception:
        return False


def _open_unmatched_count(ledger) -> Optional[int]:
    if ledger is None or not hasattr(ledger, 'list_unmatched_legs'):
        return None
    try:
        from runtime_risk_alerts import _open_unmatched_items
        return len(_open_unmatched_items(ledger))
    except Exception:
        return None


def _blocking_halts(runtime: dict) -> List[str]:
    blocks: List[str] = []
    if runtime.get('_margin_halt_open'):
        blocks.append('margin')
    if runtime.get('_journal_halt_open'):
        blocks.append('journal')
    if runtime.get('_spread_reconcile_halt'):
        blocks.append('spread_reconcile')
    if runtime.get('_strangle_reconcile_halt'):
        blocks.append('strangle_reconcile')
    if runtime.get('_position_csv_halt_open'):
        blocks.append('position_csv')
    return blocks


def build_runtime_health_summary(
    conn,
    config: dict = None,
    ledger=None,
    *,
    feishu_paused: Optional[bool] = None,
) -> Dict[str, Any]:
    """Build a structured health snapshot from conn runtime + cached metrics."""
    config = config or {}
    runtime = _runtime(conn)
    metrics = runtime.get('_daily_heartbeat_metrics') or {}
    paused = _resolve_feishu_paused(feishu_paused)
    maintenance = is_maintenance_mode(config)

    td_ok = bool(getattr(conn, 'td_logined', False)) if conn is not None else False
    md_ok = bool(getattr(conn, 'md_logined', False)) if conn is not None else False
    quarantine = bool(getattr(conn, '_reconnect_quarantine', False)) if conn else False
    blocking = _blocking_halts(runtime)

    spread_filled = metrics.get('spread_filled')
    spread_limit = metrics.get('spread_daily_limit')
    if spread_limit is None:
        spread_limit = config.get('daily_trade_limit')
    daily_limit_hit = (
        spread_filled is not None
        and spread_limit is not None
        and int(spread_filled) >= int(spread_limit)
    )

    can_auto_trade = (
        not paused
        and not maintenance
        and td_ok
        and md_ok
        and not quarantine
    )
    can_auto_open = can_auto_trade and not blocking and not daily_limit_hit

    block_reasons: List[str] = []
    if paused:
        block_reasons.append('飞书暂停')
    if maintenance:
        block_reasons.append('维护模式')
    if not td_ok or not md_ok:
        block_reasons.append('CTP未登录')
    if quarantine:
        block_reasons.append('重连隔离')
    for name in blocking:
        block_reasons.append(f'{name}_halt')
    if daily_limit_hit:
        block_reasons.append('日限达上限')

    started = config.get('_process_started_at') or runtime.get('_process_started_at')
    uptime_sec = None
    if started:
        uptime_sec = int(max(0.0, time.time() - float(started)))

    return {
        'can_auto_trade': can_auto_trade,
        'can_auto_open': can_auto_open,
        'block_reasons': block_reasons,
        'feishu_paused': paused,
        'maintenance_mode': maintenance,
        'quarantine': quarantine,
        'td_logined': td_ok,
        'md_logined': md_ok,
        'halt_margin': bool(runtime.get('_margin_halt_open')),
        'halt_journal': bool(runtime.get('_journal_halt_open')),
        'halt_spread_reconcile': bool(runtime.get('_spread_reconcile_halt')),
        'halt_strangle_reconcile': bool(runtime.get('_strangle_reconcile_halt')),
        'halt_position_csv': bool(runtime.get('_position_csv_halt_open')),
        'position_csv_halt_reason': str(
            runtime.get('_position_csv_halt_reason', '') or '',
        ),
        'journal_halt_reason': str(runtime.get('_journal_halt_reason', '') or ''),
        'loop_errors': int(runtime.get('_metric_loop_error_count', 0) or 0),
        'slow_rounds': int(runtime.get('_metric_slow_round_count', 0) or 0),
        'last_round_sec': runtime.get('_metric_last_round_sec'),
        'last_scan_end_ts': runtime.get('_health_last_scan_end_ts'),
        'last_ctp_positions_ok': runtime.get('_health_last_ctp_positions_ok'),
        'last_ctp_positions_ts': runtime.get('_health_last_ctp_positions_ts'),
        'last_ctp_positions_err': str(
            runtime.get('_health_last_ctp_positions_err', '') or '',
        ),
        'last_ledger_write_ok': runtime.get('_health_last_ledger_write_ok'),
        'last_ledger_write_ts': runtime.get('_health_last_ledger_write_ts'),
        'config_drift_detected_at': runtime.get('_config_drift_last_detected_at'),
        'config_drift_mtime': runtime.get('_config_drift_last_detected_mtime'),
        'uptime_sec': uptime_sec,
        'spread_filled': spread_filled,
        'spread_daily_limit': spread_limit,
        'strangle_buy_spent': metrics.get('strangle_buy_spent'),
        'strangle_buy_limit': metrics.get('strangle_buy_limit'),
        'margin_used': metrics.get('margin_used') or runtime.get('_last_margin_total'),
        'margin_limit': metrics.get('margin_limit') or float(
            config.get('global_margin_limit', 0) or 0,
        ),
        'open_unmatched': metrics.get('open_unmatched'),
        'pid': os.getpid(),
    }


def format_runtime_health_text(
    summary: Dict[str, Any],
    *,
    compact: bool = False,
    restart_reason: str = '',
) -> str:
    """Human-readable multi-line summary for Feishu / logs."""
    lines: List[str] = []

    trade_state = '可自动交易' if summary.get('can_auto_trade') else '不可自动交易'
    open_state = '可新开' if summary.get('can_auto_open') else '禁新开'
    lines.append(f'状态: {trade_state} | {open_state}')
    reasons = summary.get('block_reasons') or []
    if reasons:
        lines.append(f'阻断: {", ".join(reasons)}')

    lines.append(
        ' | '.join([
            f"飞书暂停={_flag(summary.get('feishu_paused'))}",
            f"维护模式={_flag(summary.get('maintenance_mode'))}",
            f"quarantine={_flag(summary.get('quarantine'))}",
        ]),
    )
    lines.append(
        ' | '.join([
            f"margin_halt={_flag(summary.get('halt_margin'))}",
            f"journal_halt={_flag(summary.get('halt_journal'))}",
            f"价差对账halt={_flag(summary.get('halt_spread_reconcile'))}",
            f"宽跨对账halt={_flag(summary.get('halt_strangle_reconcile'))}",
            f"CSV损坏halt={_flag(summary.get('halt_position_csv'))}",
            f"loop_errors={int(summary.get('loop_errors') or 0)}",
        ]),
    )

    if restart_reason:
        lines.append(f'上次启动原因: {restart_reason}')

    spread_filled = summary.get('spread_filled')
    spread_limit = summary.get('spread_daily_limit')
    if spread_filled is not None and spread_limit is not None:
        lines.append(f'价差成交 {spread_filled}/{spread_limit} 笔（日限）')
    elif spread_limit is not None:
        lines.append(f'价差日限 {spread_limit}（本轮尚未刷新成交笔数）')

    str_buy = summary.get('strangle_buy_spent')
    str_limit = summary.get('strangle_buy_limit')
    if str_buy is not None and str_limit is not None:
        lines.append(f'宽跨买入 {float(str_buy):.0f}/{float(str_limit):.0f} 元')

    margin_used = summary.get('margin_used')
    margin_limit = summary.get('margin_limit')
    if margin_used is not None and margin_limit:
        lines.append(
            f'保证金占用 {float(margin_used):.0f}/{float(margin_limit):.0f} 元',
        )
    elif margin_limit and float(margin_limit) > 0:
        lines.append(f'保证金限额 {float(margin_limit):.0f} 元（占用待下轮刷新）')

    open_unmatched = summary.get('open_unmatched')
    if open_unmatched is not None and int(open_unmatched) > 0:
        lines.append(f'宽跨开仓类未配对腿 {int(open_unmatched)} 条')

    if summary.get('halt_position_csv') and summary.get('position_csv_halt_reason'):
        lines.append(f'CSV损坏: {summary["position_csv_halt_reason"]}')

    if not compact:
        last_round = summary.get('last_round_sec')
        if last_round is not None:
            lines.append(f'最近一轮扫描耗时 {float(last_round):.1f}s')
        lines.append(
            f'最近完整扫描结束: {_fmt_ts(summary.get("last_scan_end_ts"))}',
        )
        ctp_ok = summary.get('last_ctp_positions_ok')
        if ctp_ok is not None:
            ctp_txt = '成功' if ctp_ok else '失败'
            err = summary.get('last_ctp_positions_err') or ''
            suffix = f' ({err})' if err and not ctp_ok else ''
            lines.append(
                f'最近持仓查询: {ctp_txt} @ '
                f'{_fmt_ts(summary.get("last_ctp_positions_ts"))}{suffix}',
            )
        ledger_ok = summary.get('last_ledger_write_ok')
        if ledger_ok is not None:
            ledger_txt = '成功' if ledger_ok else '失败'
            lines.append(
                f'最近 ledger 写入: {ledger_txt} @ '
                f'{_fmt_ts(summary.get("last_ledger_write_ts"))}',
            )
        if summary.get('config_drift_detected_at'):
            lines.append(
                f'配置 drift 检测: {_fmt_ts(summary.get("config_drift_detected_at"))}',
            )
        uptime = summary.get('uptime_sec')
        if uptime is not None:
            lines.append(f'进程运行 {uptime}s（pid={summary.get("pid")}）')

    return '\n'.join(lines)


def format_runtime_health_heartbeat_lines(summary: Dict[str, Any]) -> List[str]:
    """Key=value lines appended to the main-loop heartbeat file."""
    def _bool01(value) -> str:
        return '1' if value else '0'

    lines = [
        (
            f'can_auto_trade={_bool01(summary.get("can_auto_trade"))} '
            f'can_auto_open={_bool01(summary.get("can_auto_open"))} '
            f'feishu_paused={_bool01(summary.get("feishu_paused"))} '
            f'maintenance_mode={_bool01(summary.get("maintenance_mode"))}'
        ),
        (
            f'halt_margin={_bool01(summary.get("halt_margin"))} '
            f'halt_journal={_bool01(summary.get("halt_journal"))} '
            f'halt_spread_reconcile={_bool01(summary.get("halt_spread_reconcile"))} '
            f'halt_strangle_reconcile={_bool01(summary.get("halt_strangle_reconcile"))} '
            f'halt_position_csv={_bool01(summary.get("halt_position_csv"))}'
        ),
        (
            f'loop_errors={int(summary.get("loop_errors") or 0)} '
            f'slow_rounds={int(summary.get("slow_rounds") or 0)}'
        ),
    ]
    last_round = summary.get('last_round_sec')
    if last_round is not None:
        lines.append(f'last_round_sec={float(last_round):.1f}')
    if summary.get('last_scan_end_ts'):
        lines.append(
            f'last_scan_end_ts={_fmt_ts(summary.get("last_scan_end_ts"))}',
        )
    ctp_ok = summary.get('last_ctp_positions_ok')
    if ctp_ok is not None:
        lines.append(
            f'last_ctp_positions_ok={_bool01(ctp_ok)} '
            f'last_ctp_positions_ts={_fmt_ts(summary.get("last_ctp_positions_ts"))}',
        )
    spread_filled = summary.get('spread_filled')
    spread_limit = summary.get('spread_daily_limit')
    if spread_filled is not None and spread_limit is not None:
        lines.append(f'spread_filled={int(spread_filled)}/{int(spread_limit)}')
    open_unmatched = summary.get('open_unmatched')
    if open_unmatched is not None:
        lines.append(f'open_unmatched={int(open_unmatched)}')
    return lines
