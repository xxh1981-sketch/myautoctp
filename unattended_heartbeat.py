"""无人值守心跳：主循环心跳文件 + 每日飞书报平安。

- 心跳文件：主循环每轮覆写多行状态（mtime + 内容供外部 watchdog）。
  merged_main.py 的进程内重启循环只覆盖 Python 未捕获异常，覆盖不了整进程
  挂死 / 被 OS kill；外部 watchdog（计划任务 / 监控脚本）检查 mtime 即可发现。
- 每日报平安：每天首次到达 ``daily_heartbeat_hour`` 后向飞书发送一条运行状态
  摘要。各类异常告警可能因飞书链路故障而静默丢失，「没收到报平安」可反向
  发现程序或通知链路的整体故障。发送成功才写日期标记文件（防重发 / 防重启
  风暴刷屏）；失败按 ``_RETRY_INTERVAL_SEC`` 重试。
- 重启原因文件：进程启动/重启/退出时写入，供报平安与排障引用。

三者均为纯可观测性组件：不改变任何 halt / 交易语义，失败仅告警。
"""

from __future__ import annotations

import os
import time
from datetime import date, datetime
from typing import Optional

# 报平安发送失败后的最小重试间隔（秒），防止飞书长时间不可用时每轮刷 warning。
_RETRY_INTERVAL_SEC = 600.0
# 心跳文件写失败（磁盘满 / 权限）告警节流（秒）。
_TOUCH_WARN_INTERVAL_SEC = 600.0
_last_touch_warn = 0.0


def heartbeat_paths(config: dict) -> tuple:
    """(heartbeat_file, daily_marker_file)；空字符串表示禁用对应功能。"""
    hb = str(config.get('heartbeat_file') or '')
    marker = str(config.get('daily_heartbeat_marker_file') or '')
    return hb, marker


def restart_reason_path(config: dict) -> str:
    return str(config.get('restart_reason_file') or 'data/restart_reason.txt')


def _writable_path(path: str) -> bool:
    """pytest 下禁止写生产 data/（与 atomic_io 的 data_path_guard 语义一致）。"""
    from data_path_guard import is_repo_data_path, under_pytest
    return not (under_pytest() and is_repo_data_path(path))


def _write_text_file(path: str, content: str) -> bool:
    if not path or not _writable_path(path):
        return False
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)
    return True


def _bool_flag(value) -> str:
    return '1' if value else '0'


def _build_running_heartbeat_lines(
    conn=None,
    *,
    process_started_at: Optional[float] = None,
) -> list[str]:
    runtime = getattr(conn, '_runtime_state', None) or {} if conn is not None else {}
    now = time.time()
    lines = [
        f'timestamp={datetime.now().isoformat(timespec="seconds")} pid={os.getpid()}',
        'status=RUNNING',
    ]
    started = process_started_at
    if started is None:
        started = runtime.get('_process_started_at')
    if started:
        lines.append(f'uptime_sec={int(max(0.0, now - float(started)))}')
    lines.extend([
        (
            f'halt_margin={_bool_flag(runtime.get("_margin_halt_open"))} '
            f'halt_journal={_bool_flag(runtime.get("_journal_halt_open"))} '
            f'halt_spread_reconcile={_bool_flag(runtime.get("_spread_reconcile_halt"))} '
            f'halt_strangle_reconcile={_bool_flag(runtime.get("_strangle_reconcile_halt"))}'
        ),
        (
            f'loop_errors={int(runtime.get("_metric_loop_error_count", 0) or 0)} '
            f'slow_rounds={int(runtime.get("_metric_slow_round_count", 0) or 0)}'
        ),
    ])
    last_round = runtime.get('_metric_last_round_sec')
    if last_round is not None:
        lines.append(f'last_round_sec={float(last_round):.1f}')
    return lines


def touch_heartbeat_file(
    config: dict,
    logger=None,
    *,
    conn=None,
    process_started_at: Optional[float] = None,
) -> bool:
    """每轮覆写心跳文件；任何失败不抛出（节流 warning）。"""
    global _last_touch_warn
    path, _ = heartbeat_paths(config)
    if not path:
        return False
    try:
        lines = _build_running_heartbeat_lines(
            conn,
            process_started_at=process_started_at,
        )
        if not _write_text_file(path, '\n'.join(lines) + '\n'):
            return False
        return True
    except Exception as e:
        now = time.time()
        if logger and now - _last_touch_warn >= _TOUCH_WARN_INTERVAL_SEC:
            _last_touch_warn = now
            logger.warning(f'[心跳] 心跳文件写入失败（磁盘/权限？）: {e}')
        return False


def write_stopped_heartbeat(
    config: dict,
    reason: str = 'SIGTERM',
    logger=None,
) -> bool:
    """进程正常退出时写入 STOPPED 状态，供外部 watchdog 静默跳过。"""
    path, _ = heartbeat_paths(config)
    if not path:
        return False
    try:
        lines = [
            'status=STOPPED',
            f'timestamp={datetime.now().isoformat(timespec="seconds")} pid={os.getpid()}',
            f'reason={reason}',
        ]
        if not _write_text_file(path, '\n'.join(lines) + '\n'):
            return False
        if logger:
            logger.info(f'[心跳] 已写入 STOPPED 心跳（reason={reason}）')
        return True
    except Exception as e:
        if logger:
            logger.warning(f'[心跳] STOPPED 心跳写入失败: {e}')
        return False


def write_restart_reason(
    config: dict,
    reason: str,
    detail: str = '',
    logger=None,
) -> bool:
    """写入最近一次启动/重启/退出原因（纯可观测性）。"""
    path = restart_reason_path(config)
    if not path:
        return False
    try:
        lines = [
            f'reason={reason}',
            f'timestamp={datetime.now().isoformat(timespec="seconds")}',
            f'pid={os.getpid()}',
        ]
        if detail:
            lines.append(f'detail={detail}')
        if not _write_text_file(path, '\n'.join(lines) + '\n'):
            return False
        if logger:
            logger.info(f'[心跳] 重启原因: {reason}' + (f' ({detail})' if detail else ''))
        return True
    except Exception as e:
        if logger:
            logger.warning(f'[心跳] 重启原因写入失败: {e}')
        return False


def read_restart_reason_summary(config: dict) -> str:
    """读取重启原因文件首行，供报平安展示；失败返回空串。"""
    path = restart_reason_path(config)
    if not path or not os.path.isfile(path):
        return ''
    try:
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line.startswith('reason='):
                    return line.split('=', 1)[1]
    except OSError:
        pass
    return ''


def _read_marker(path: str) -> str:
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return f.read().strip()
    except OSError:
        return ''


def _open_unmatched_count(ledger) -> Optional[int]:
    if ledger is None or not hasattr(ledger, 'list_unmatched_legs'):
        return None
    try:
        from runtime_risk_alerts import _open_unmatched_items
        return len(_open_unmatched_items(ledger))
    except Exception:
        return None


def stash_daily_heartbeat_metrics(
    conn,
    *,
    spread_filled: int,
    spread_daily_limit: int,
    strangle_buy_spent: float,
    strangle_buy_limit: float,
    ledger=None,
    margin_limit: float = 0,
) -> None:
    """Persist last-round trading metrics for the next daily heartbeat message."""
    runtime = getattr(conn, '_runtime_state', None)
    if runtime is None:
        return
    margin_used = runtime.get('_last_margin_total')
    runtime['_daily_heartbeat_metrics'] = {
        'spread_filled': int(spread_filled),
        'spread_daily_limit': int(spread_daily_limit),
        'strangle_buy_spent': float(strangle_buy_spent),
        'strangle_buy_limit': float(strangle_buy_limit),
        'margin_used': margin_used,
        'margin_limit': float(margin_limit or 0),
        'open_unmatched': _open_unmatched_count(ledger),
    }


def _status_summary(conn, config: dict = None, ledger=None) -> str:
    runtime = getattr(conn, '_runtime_state', None) or {}
    metrics = runtime.get('_daily_heartbeat_metrics') or {}

    def _flag(value) -> str:
        return '是' if value else '否'

    lines = [
        ' | '.join([
            f"quarantine={_flag(getattr(conn, '_reconnect_quarantine', False))}",
            f"margin_halt={_flag(runtime.get('_margin_halt_open'))}",
            f"journal_halt={_flag(runtime.get('_journal_halt_open'))}",
            f"价差对账halt={_flag(runtime.get('_spread_reconcile_halt'))}",
            f"宽跨对账halt={_flag(runtime.get('_strangle_reconcile_halt'))}",
            f"loop_errors={int(runtime.get('_metric_loop_error_count', 0) or 0)}",
        ]),
    ]

    restart_reason = read_restart_reason_summary(config or {})
    if restart_reason:
        lines.append(f'上次启动原因: {restart_reason}')

    spread_filled = metrics.get('spread_filled')
    spread_limit = metrics.get('spread_daily_limit')
    if spread_filled is not None and spread_limit is not None:
        lines.append(f'价差成交 {spread_filled}/{spread_limit} 笔（日限）')
    elif config is not None:
        lines.append(
            f"价差日限 {config.get('daily_trade_limit', '?')}（本轮尚未刷新成交笔数）"
        )

    str_buy = metrics.get('strangle_buy_spent')
    str_limit = metrics.get('strangle_buy_limit')
    if str_buy is not None and str_limit is not None:
        lines.append(f'宽跨买入 {str_buy:.0f}/{str_limit:.0f} 元')

    margin_used = metrics.get('margin_used')
    margin_limit = metrics.get('margin_limit')
    if margin_limit is None and config is not None:
        margin_limit = float(config.get('global_margin_limit', 0) or 0)
    if margin_used is not None and margin_limit:
        lines.append(f'保证金占用 {float(margin_used):.0f}/{float(margin_limit):.0f} 元')
    elif margin_limit and float(margin_limit) > 0:
        lines.append(f'保证金限额 {float(margin_limit):.0f} 元（占用待下轮刷新）')

    open_unmatched = metrics.get('open_unmatched')
    if open_unmatched is None:
        open_unmatched = _open_unmatched_count(ledger)
    if open_unmatched is not None and int(open_unmatched) > 0:
        lines.append(f'宽跨开仓类未配对腿 {int(open_unmatched)} 条')

    return '\n'.join(lines)


def maybe_send_daily_heartbeat(
    conn, config: dict, logger=None, ledger=None,
) -> bool:
    """到点后每天发送一次报平安；仅发送成功才写日期标记。永不抛出。"""
    try:
        if not bool(config.get('daily_heartbeat_enabled', True)):
            return False
        _, marker = heartbeat_paths(config)
        if not marker or not _writable_path(marker):
            return False
        hour = int(config.get('daily_heartbeat_hour', 9) or 0)
        if datetime.now().hour < hour:
            return False
        today = date.today().isoformat()
        if _read_marker(marker) == today:
            return False

        # 注意不可写成 `... or {}`：_runtime_state 为空 dict 时会被换成新字典，
        # 节流标记将写不进 conn，导致失败后每轮重试。
        runtime = getattr(conn, '_runtime_state', None)
        if runtime is None:
            runtime = {}
        now = time.time()
        last_attempt = float(runtime.get('_daily_heartbeat_last_attempt', 0.0) or 0.0)
        if now - last_attempt < _RETRY_INTERVAL_SEC:
            return False
        runtime['_daily_heartbeat_last_attempt'] = now

        msg = (
            f'💓 **AutoCTP 每日报平安** {today}\n\n'
            f'主循环运行中（pid={os.getpid()}）。\n'
            f'{_status_summary(conn, config=config, ledger=ledger)}\n\n'
            '若某日未收到本消息，请检查程序进程与飞书通知链路。'
        )
        try:
            from auto_feishu import send_feishu_message
            ok = bool(send_feishu_message(msg, config=config))
        except Exception as e:
            if logger:
                logger.warning(f'[心跳] 每日报平安飞书发送异常，稍后重试: {e}')
            return False
        if not ok:
            if logger:
                logger.warning('[心跳] 每日报平安飞书发送未成功，稍后重试')
            return False

        from atomic_io import atomic_write_text
        atomic_write_text(marker, today + '\n')
        if logger:
            logger.info(f'[心跳] 每日报平安已发送（{today}）')
        return True
    except Exception as e:
        if logger:
            logger.warning(f'[心跳] 每日报平安处理异常: {e}')
        return False
