"""Startup self-checks for merged_main (margin, target_months).

Kept separate from merged_main so unit tests do not need the full autotrade
import chain (VIXEngine, CTP connection, etc.).
"""

from __future__ import annotations

import sys
from typing import Any, Dict, List

from margin_check import check_margin_status

_DEFAULT_STARTUP_MARGIN_ALERT_COOLDOWN_SEC = 1800


def _notify_startup_margin(
    config: dict,
    logger,
    runtime: dict,
    body: str,
) -> None:
    """Send feishu for startup margin issues; cooldown avoids spam on restart loops."""
    import time

    cooldown = float(
        config.get('startup_margin_alert_cooldown_sec')
        or _DEFAULT_STARTUP_MARGIN_ALERT_COOLDOWN_SEC
    )
    now = time.time()
    last = float(runtime.get('_last_startup_margin_alert_time') or 0.0)
    if now - last < cooldown:
        return
    runtime['_last_startup_margin_alert_time'] = now
    try:
        from auto_feishu import send_feishu_message
        send_feishu_message(
            f'⚠️ **AutoCTP 启动保证金告警**\n\n{body}',
            config=config,
        )
    except Exception as notify_err:
        logger.warning(
            '启动保证金飞书通知失败: %s', notify_err, exc_info=True,
        )


def apply_startup_margin(
    conn,
    config: dict,
    logger,
    ledger,
    str_cfg: dict,
) -> bool:
    """Apply startup margin check.

    Returns ``True`` to proceed into the main loop in all cases. ``over_limit``
    and ``unknown`` (when limit enabled) set ``_margin_halt_open`` so opens are
    blocked while closes still run — same semantics as the periodic main-loop
    margin recheck.
    """
    status, reason = check_margin_status(conn, config, logger, context='启动')
    runtime = conn._runtime_state
    limit = config.get('global_margin_limit', 0)

    if status == 'ok':
        runtime['_margin_halt_open'] = False
        runtime['_margin_halt_reason'] = ''
        return True
    if status == 'over_limit':
        halt_reason = f'{reason} (限额 {limit})' if limit else reason
        runtime['_margin_halt_open'] = True
        runtime['_margin_halt_reason'] = halt_reason
        logger.error(
            '启动保证金超限（%s）；已设置 margin_halt_open=True，'
            '仍进入主循环（允许平仓，禁新开/再平衡）',
            reason,
        )
        _notify_startup_margin(
            config,
            logger,
            runtime,
            f'{halt_reason}\n\n'
            '程序将继续运行并扫描平仓；请减仓或调高限额后等待主循环复检解除。',
        )
        return True

    # unknown: cold-start prev should be conservative when margin limit is enabled
    if limit > 0:
        runtime['_margin_halt_open'] = True
        runtime['_margin_halt_reason'] = (
            f'启动保证金无法判定（{reason}），保守禁新开直至复检成功'
        )
        logger.warning(
            f'启动保证金检查无法判定（{reason}）；'
            f'已保守设置 margin_halt_open=True（限额 {limit}），'
            '主循环复检成功后解除；不阻塞启动本身'
        )
        _notify_startup_margin(
            config,
            logger,
            runtime,
            f'启动保证金无法判定（{reason}，限额 {limit}）。\n\n'
            '已保守禁新开；主循环复检成功后将自动解除。',
        )
    else:
        logger.warning(
            f'启动保证金检查无法判定（{reason}），global_margin_limit=0 未启用风控'
        )
    return True


def audit_target_months(
    conn,
    config: dict,
    logger,
    spread_info: List[Dict[str, Any]],
    strangle_info: List[Dict[str, Any]],
    *,
    send_feishu=None,
) -> None:
    """Warn (and optionally exit) when conn.target_months is empty for tradeinfo symbols."""
    from order_whitelist_guard import audit_target_months_coverage

    missing = audit_target_months_coverage(conn, spread_info, strangle_info)
    if not missing:
        return
    msg = (
        f'以下品种 conn.target_months 为空，发单月白名单不校验邻月: {missing}。'
        '建议检查 tradeinfo 与 CTP 连接初始化。'
    )
    logger.error('[启动自检] %s', msg)
    if send_feishu is None:
        try:
            from auto_feishu import send_feishu_message
            send_feishu = send_feishu_message
        except Exception:
            send_feishu = None
    if send_feishu is not None:
        try:
            send_feishu(
                f'⚠️ **AutoCTP 启动自检告警**\n\n{msg}',
                config=config,
            )
        except Exception as notify_err:
            logger.warning(
                'target_months 自检飞书通知失败: %s', notify_err, exc_info=True,
            )
    if config.get('fail_fast_on_empty_target_months', False):
        logger.error('[启动自检] fail_fast_on_empty_target_months=true，拒绝启动')
        sys.exit(5)
