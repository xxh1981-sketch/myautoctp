"""单进程双策略主循环（价差 + 宽跨）。"""

import time

import auto_processor  # 通过模块属性访问 process_symbol，确保 spread_ledger_execution 的 patch 生效
from auto_initializer import manage_future_price_readiness

import margin_check  # 通过模块属性访问 check_margin_status，便于测试 patch 与未来扩展
from merged_strategy_logger import strategy_logger, strategy_logging
from merged_vix_cache import begin_round_vix_cache, wrap_vix_engine


def _update_bool_metric(runtime: dict, name: str, active: bool, now: float) -> None:
    active_key = f'_metric_{name}_active'
    since_key = f'_metric_{name}_since'
    count_key = f'_metric_{name}_enter_count'
    total_key = f'_metric_{name}_total_sec'
    prev = bool(runtime.get(active_key, False))
    if active and not prev:
        runtime[count_key] = int(runtime.get(count_key, 0) or 0) + 1
        runtime[since_key] = now
    elif not active and prev:
        since = float(runtime.get(since_key, now) or now)
        runtime[total_key] = float(runtime.get(total_key, 0.0) or 0.0) + max(0.0, now - since)
        runtime[since_key] = 0.0
    elif active and not runtime.get(since_key):
        runtime[since_key] = now
    runtime[active_key] = bool(active)


def _metric_total_sec(runtime: dict, name: str, now: float) -> float:
    total = float(runtime.get(f'_metric_{name}_total_sec', 0.0) or 0.0)
    if bool(runtime.get(f'_metric_{name}_active', False)):
        since = float(runtime.get(f'_metric_{name}_since', now) or now)
        total += max(0.0, now - since)
    return total


def _emit_runtime_metrics_if_due(conn, config: dict, logger, now: float) -> None:
    runtime = conn._runtime_state
    interval = float(config.get('metrics_log_interval_sec', 300) or 300)
    last = float(runtime.get('_metrics_last_log_ts', 0.0) or 0.0)
    if now - last < interval:
        return
    runtime['_metrics_last_log_ts'] = now
    logger.info(
        '[运行指标] '
        f'quarantine={int(runtime.get("_metric_quarantine_enter_count", 0))}次/'
        f'{_metric_total_sec(runtime, "quarantine", now):.0f}s '
        f'journal_halt={int(runtime.get("_metric_journal_halt_enter_count", 0))}次/'
        f'{_metric_total_sec(runtime, "journal_halt", now):.0f}s '
        f'spread_halt={int(runtime.get("_metric_spread_halt_enter_count", 0))}次/'
        f'{_metric_total_sec(runtime, "spread_halt", now):.0f}s '
        f'strangle_halt={int(runtime.get("_metric_strangle_halt_enter_count", 0))}次/'
        f'{_metric_total_sec(runtime, "strangle_halt", now):.0f}s '
        f'loop_errors={int(runtime.get("_metric_loop_error_count", 0))}'
    )


def _prefetch_round_data(conn, logger) -> tuple:
    """Single CTP query of positions + trades shared across reconcile passes."""
    positions = None
    trades = None
    try:
        positions = conn.query_positions_sync(timeout=10) or []
    except Exception as e:
        if logger:
            logger.debug(f'[reconcile] positions prefetch failed: {e}')
    try:
        if hasattr(conn, 'query_trades_sync'):
            trades = conn.query_trades_sync(timeout=12, use_cache=False)
    except Exception as e:
        if logger:
            logger.debug(f'[reconcile] trades prefetch failed: {e}')
    return positions, trades


def _run_reconcile(
    conn,
    ledger,
    spread_tradeinfo,
    strangle_tradeinfo,
    spread_logger,
    strangle_logger,
    config,
    str_cfg,
    dual,
):
    from straggle_reconcile import reconcile_strangle_positions
    from strangle_reconcile_dual import reconcile_strangle_positions_dual

    positions, trades = _prefetch_round_data(conn, strangle_logger)

    str_symbols = {it['future'].lower() for it in strangle_tradeinfo}
    runtime = conn._runtime_state
    # P7: 与 spread reconcile 对称——异常时保守 halt（开仓 / 再平衡禁用，
    # 平仓仍允许），避免在状态不可信时让主循环继续开仓。沿用上一轮 issues
    # 但加一条异常说明。
    try:
        if dual.get('exclude_spread_from_strangle_reconcile', True):
            halt, issues = reconcile_strangle_positions_dual(
                conn, ledger, str_symbols, spread_tradeinfo,
                strangle_logger, config=config,
                positions=positions, trades=trades,
            )
        else:
            halt, issues = reconcile_strangle_positions(
                conn, ledger, str_symbols, strangle_logger, config=config,
            )
    except Exception as e:
        strangle_logger.warning(
            f'[strangle-reconcile] 异常，保守 halt 开仓: {e}', exc_info=True,
        )
        halt = True
        prev_issues = list(runtime.get('_strangle_reconcile_issues') or [])
        issues = [f'reconcile 异常: {e}'] + prev_issues[:4]

    runtime['_strangle_reconcile_halt'] = halt
    runtime['_strangle_reconcile_issues'] = issues
    if str_cfg.get('pause_open_on_reconcile_mismatch', True):
        _sync_strangle_open_halt(conn, ledger, str_cfg)

    spread_halt = False
    spread_issues: list = []
    if dual.get('spread_execution_from_ledger', True):
        try:
            from spread_reconcile import reconcile_spread_positions
            from spread_ledger_execution import set_spread_open_halt

            spread_halt, spread_issues = reconcile_spread_positions(
                conn, spread_tradeinfo, spread_logger, config=config,
                positions=positions, trades=trades,
            )
            if dual.get('pause_spread_open_on_reconcile_mismatch', True):
                set_spread_open_halt(
                    conn,
                    spread_halt,
                    '; '.join(spread_issues[:5]) if spread_halt else '',
                )
        except Exception as e:
            # 与 strangle reconcile P7 对称：异常时保守 halt 开仓 / 再平衡，
            # 沿用上一轮 issues 并加一条异常说明，便于排障时看到历史上下文。
            spread_logger.warning(
                f'[spread-reconcile] 异常，保守 halt 开仓: {e}', exc_info=True,
            )
            prev_issues = list(runtime.get('_spread_reconcile_issues') or [])
            spread_halt = True
            spread_issues = [f'reconcile 异常: {e}'] + prev_issues[:4]
            if dual.get('pause_spread_open_on_reconcile_mismatch', True):
                from spread_ledger_execution import set_spread_open_halt
                set_spread_open_halt(
                    conn, True, '; '.join(spread_issues[:5]),
                )

    runtime['_spread_reconcile_halt'] = spread_halt
    runtime['_spread_reconcile_issues'] = spread_issues
    return halt, issues, spread_halt, spread_issues


def _sync_strangle_open_halt(conn, ledger, str_cfg: dict) -> None:
    """
    Set strangle ledger.open_halted from the union of reconcile halt and
    margin halt, with a precedence-aware reason.

    Reason precedence: reconcile_issues > margin_reason.
    Idempotent: only writes when state actually changes.
    """
    if not str_cfg.get('pause_open_on_reconcile_mismatch', True):
        return
    runtime = conn._runtime_state
    recon_halt = bool(runtime.get('_strangle_reconcile_halt', False))
    recon_issues = list(runtime.get('_strangle_reconcile_issues') or [])
    margin_halt = bool(runtime.get('_margin_halt_open', False))
    margin_reason = runtime.get('_margin_halt_reason') or '保证金超限，暂停新开'
    journal_halt = bool(runtime.get('_journal_halt_open', False))
    journal_reason = runtime.get('_journal_halt_reason') or 'journal存在未完成入账，暂停新开'

    target_halt = recon_halt or margin_halt or journal_halt
    if recon_halt:
        target_reason = '; '.join(recon_issues[:5])
    elif journal_halt:
        target_reason = journal_reason
    elif margin_halt:
        target_reason = margin_reason
    else:
        target_reason = ''

    current_halt = ledger.is_open_halted()
    current_reason = (
        ledger.get_open_halt_reason()
        if hasattr(ledger, 'get_open_halt_reason') else ''
    )
    if current_halt == target_halt and current_reason == target_reason:
        return
    ledger.set_open_halt(target_halt, target_reason)


def _quarantine_close_only_enabled(config: dict) -> bool:
    q_cfg = config.get('reconnect_quarantine') or {}
    return bool(q_cfg.get('close_only_enabled', False))


def _can_run_quarantine_close_only(conn, config: dict) -> tuple:
    """Strict guard for quarantine close-only mode.

    This mode is intentionally conservative: it is off by default and only
    activates when login, code table, and local pending-order state are all
    healthy enough to reduce the chance of side effects.
    """
    if not _quarantine_close_only_enabled(config):
        return False, 'disabled'
    if not conn._reconnect_quarantine:
        return False, 'not_quarantine'
    if not (conn.td_logined and conn.md_logined):
        return False, 'channels_not_ready'
    if not getattr(conn, 'code_table_loaded', False):
        return False, 'code_table_not_ready'
    pending = len(getattr(conn, 'pending_orders', {}) or {})
    if pending > 0:
        return False, f'pending_orders={pending}'
    return True, 'ok'


def _run_quarantine_close_only_round(
    conn,
    spread_tradeinfo: list,
    strangle_tradeinfo: list,
    tradeinfo_by_key: dict,
    round_vix_engine,
    config: dict,
    logger,
    ledger,
    str_executor,
    loop_interval: float,
) -> None:
    from spread_ledger_execution import _spread_close_only
    from straggle_processor import process_strangle_symbol
    from strangle_rebalance_close_only import run_close_only_rebalance

    logger.warning('[重连] 隔离期安全降级：仅执行平仓/close-only，再平衡开仓暂停')
    conn._runtime_state['_allow_quarantine_close_only'] = True
    try:
        for item in spread_tradeinfo:
            with strategy_logging(conn, logger, 'spread') as s_logger:
                try:
                    _spread_close_only(conn, item, round_vix_engine, config, s_logger)
                except Exception as e:
                    s_logger.error(f"[{item['future']}] 隔离期close-only异常: {e}", exc_info=True)

        with strategy_logging(conn, logger, 'strangle') as s_logger:
            for item in strangle_tradeinfo:
                try:
                    process_strangle_symbol(
                        conn,
                        item,
                        round_vix_engine,
                        config,
                        s_logger,
                        ledger,
                        str_executor,
                        circuit_breaker=None,
                        allow_quarantine_close_only=True,
                    )
                except Exception as e:
                    s_logger.error(
                        f"[{item['future']}] 隔离期宽跨平仓扫描异常: {e}",
                        exc_info=True,
                    )
            try:
                handled = run_close_only_rebalance(str_executor, ledger, tradeinfo_by_key)
                if handled:
                    s_logger.info(f'隔离期close-only再平衡处理 {handled} 条')
            except Exception as e:
                s_logger.error(f'隔离期close-only再平衡异常: {e}', exc_info=True)
    finally:
        conn._runtime_state.pop('_allow_quarantine_close_only', None)
        time.sleep(loop_interval)


def run_merged_main_loop(
    conn,
    spread_tradeinfo: list,
    strangle_tradeinfo: list,
    combined_tradeinfo: list,
    vix_engine,
    config: dict,
    logger,
    ledger,
    health_checker=None,
):
    from straggle_processor import process_strangle_symbol
    from straggle_execution import StrangleExecutor
    from auto_circuit_breaker import CircuitBreaker
    from auto_health_check import HealthChecker
    from auto_feishu_command import (
        start_command_receiver,
        is_trading_paused,
        stop_command_receiver,
    )
    from auto_scheduled_reconnect import check_scheduled_full_recovery
    from auto_scheduled_pause import (
        is_connection_suspended,
        sync_connection_suspend_state,
    )
    from strategy_status_snapshot import (
        build_strategy_status_snapshot,
        format_strategy_status_message,
    )

    if health_checker is None:
        health_checker = HealthChecker(conn, config, logger)

    spread_logger = strategy_logger(logger, 'spread')
    strangle_logger = strategy_logger(logger, 'strangle')

    circuit_breaker = CircuitBreaker(conn, config, logger)
    conn._runtime_state['_circuit_breaker'] = circuit_breaker
    str_executor = StrangleExecutor(conn, config, strangle_logger, ledger)
    tradeinfo_by_key = {
        (it['future'].lower(), it['month']): it for it in strangle_tradeinfo
    }

    dual = config.get('dual_strategy') or {}
    strategy_order = dual.get('strategy_order', ['spread', 'strangle'])
    str_cfg = config.get('strangle', {})

    receiver = start_command_receiver(config)
    if receiver is not None and hasattr(receiver, 'set_status_provider'):
        def _status_provider(command_text: str) -> str:
            snapshot = conn._runtime_state.get('_strategy_status_snapshot')
            if not snapshot:
                return 'ℹ️ 策略状态尚未就绪或本轮尚未生成快照，请稍后再试。'
            return format_strategy_status_message(snapshot, command_text=command_text)

        try:
            receiver.set_status_provider(_status_provider)
        except Exception as e:
            logger.debug(f'[飞书指令] 注册状态查询 provider 失败: {e}')
    loop_interval = config.get('loop_interval', 10)
    spread_daily_limit = config.get('daily_trade_limit', 100)
    strangle_buy_limit = float(str_cfg.get('daily_buy_limit_yuan', 300000))
    fp_interval = config.get('future_price_status_interval', 60)
    reconcile_interval = float(dual.get('reconcile_interval_sec', 60))
    margin_recheck_interval = float(config.get('margin_recheck_interval_sec', 300))
    spread_limit_notified = False
    _last_unhealthy_alert_time = 0.0
    _health_alert_cooldown = config.get('health_alert_cooldown', 300)
    _last_reconcile_time = 0.0
    _last_margin_check_time = time.time()
    _last_journal_check_time = 0.0
    _consecutive_loop_errors = 0
    _max_loop_errors = int(config.get('main_loop_max_consecutive_errors', 10) or 0)
    conn._runtime_state.setdefault('_margin_halt_open', False)
    conn._runtime_state.setdefault('_margin_halt_reason', '')

    logger.info("=" * 60)
    logger.info("AutoCTP 双策略主循环")
    logger.info(f"价差 {len(spread_tradeinfo)} 品种, 宽跨 {len(strangle_tradeinfo)} 品种")
    logger.info(f"顺序: {strategy_order}, 全局 1 在途")
    logger.info(
        f"对账间隔: {reconcile_interval:.0f}s, "
        f"保证金复检: {margin_recheck_interval:.0f}s"
    )
    logger.info("=" * 60)

    try:
        while True:
            round_t0 = time.time()
            try:
                sync_connection_suspend_state(conn, config, logger)
                if check_scheduled_full_recovery(conn, config, logger):
                    time.sleep(loop_interval)
                    continue

                # 定时非交易挂起期间连接已主动释放，跳过健康检查以免刷屏
                # （断连/僵尸单/持仓校准等在挂起态均属预期）。
                session_suspended = is_connection_suspended(config)
                if not session_suspended:
                    health_report = health_checker.check_now(force=False)
                    if health_report and not health_report.get('healthy'):
                        if not health_report.get('details', {}).get(
                            'expected_suspend_offline',
                        ):
                            issues = '; '.join(health_report.get('issues', []))
                            logger.warning(f"[健康] {issues}")
                            is_reconnecting = (
                                conn._td_disconnect_notified
                                or conn._md_disconnect_notified
                            )
                            if not is_reconnecting:
                                now = time.time()
                                if (
                                    now - _last_unhealthy_alert_time
                                    >= _health_alert_cooldown
                                ):
                                    _last_unhealthy_alert_time = now
                                    try:
                                        from auto_feishu import send_feishu_message
                                        summary = health_checker.get_summary()
                                        send_feishu_message(
                                            f"🔴 **系统健康检查异常**\n\n{summary}",
                                            config=config,
                                        )
                                    except Exception as e:
                                        logger.debug(
                                            f"健康检查飞书通知失败: {e}",
                                        )

                now = time.time()
                _update_bool_metric(
                    conn._runtime_state, 'quarantine',
                    bool(conn._reconnect_quarantine), now,
                )
                _emit_runtime_metrics_if_due(conn, config, logger, now)

                if conn._reconnect_quarantine or not conn.td_logined or not conn.md_logined:
                    from auto_reconnect_recovery import check_quarantine_watchdog
                    from auto_scheduled_pause import log_main_loop_offline_skip
                    conn._runtime_state['_fill_ledger_replay_pending'] = True
                    conn._runtime_state['_force_reconcile'] = True
                    if conn._reconnect_quarantine and not is_connection_suspended(config):
                        check_quarantine_watchdog(conn, config, logger)
                    can_close_only, reason = _can_run_quarantine_close_only(conn, config)
                    if can_close_only:
                        begin_round_vix_cache(conn)
                        round_vix_engine = wrap_vix_engine(vix_engine, conn, logger)
                        _run_quarantine_close_only_round(
                            conn=conn,
                            spread_tradeinfo=spread_tradeinfo,
                            strangle_tradeinfo=strangle_tradeinfo,
                            tradeinfo_by_key=tradeinfo_by_key,
                            round_vix_engine=round_vix_engine,
                            config=config,
                            logger=logger,
                            ledger=ledger,
                            str_executor=str_executor,
                            loop_interval=loop_interval,
                        )
                        continue
                    log_main_loop_offline_skip(
                        conn, config, logger, quarantine=conn._reconnect_quarantine,
                    )
                    if _quarantine_close_only_enabled(config) and conn._reconnect_quarantine:
                        logger.info(f'[重连] 隔离期close-only未启用: {reason}')
                    try:
                        from runtime_risk_alerts import notify_quarantine_prolonged
                        notify_quarantine_prolonged(conn, config, logger)
                    except Exception as e:
                        logger.debug(f'[风控告警] quarantine: {e}')
                    time.sleep(loop_interval)
                    continue

                conn._runtime_state.pop('_suspend_skip_logged', None)

                if conn._runtime_state.pop('_fill_ledger_replay_pending', False):
                    try:
                        from fill_ledger import sync_fill_ledger_from_trades
                        sync_fill_ledger_from_trades(conn, config, logger)
                    except Exception as e:
                        logger.debug(f'[FillLedger] post-reconnect replay: {e}')

                if is_trading_paused():
                    try:
                        from runtime_risk_alerts import notify_feishu_pause_exposure
                        notify_feishu_pause_exposure(
                            conn, ledger, config, logger, paused=True,
                        )
                    except Exception as e:
                        logger.debug(f'[风控告警] feishu pause: {e}')
                    with conn._executor_lock:
                        ex = conn._active_executor
                        if ex:
                            try:
                                ex.stop_all_threads.set()
                                ex.cleanup()
                            except Exception:
                                pass
                            conn._active_executor = None
                            logger.info(
                                '[飞书暂停] 已清理在途执行器，跳过本轮（含平仓扫描）'
                            )
                    time.sleep(loop_interval)
                    continue

                manage_future_price_readiness(
                    conn, combined_tradeinfo, logger, conn._runtime_state, fp_interval,
                )

                if (
                    now - _last_journal_check_time >= reconcile_interval
                    or conn._runtime_state.pop('_force_journal_check', False)
                ):
                    _last_journal_check_time = now
                    try:
                        from fill_ledger import fill_ledger_journal_path
                        from spread_fill_sync import _journal_path as _spread_journal_path
                        from strangle_fill_sync import _journal_path as _strangle_journal_path
                        from trade_journal import scan_unresolved_pending

                        spread_j = scan_unresolved_pending(
                            _spread_journal_path(config), config,
                        )
                        strangle_j = scan_unresolved_pending(
                            _strangle_journal_path(config), config,
                        )
                        fill_j = scan_unresolved_pending(
                            fill_ledger_journal_path(config), config,
                        )
                        unresolved = (
                            int(spread_j.get('unresolved_pending', 0))
                            + int(strangle_j.get('unresolved_pending', 0))
                            + int(fill_j.get('unresolved_pending', 0))
                        )
                        malformed = (
                            int(spread_j.get('malformed_lines', 0))
                            + int(strangle_j.get('malformed_lines', 0))
                            + int(fill_j.get('malformed_lines', 0))
                        )
                        runtime = conn._runtime_state
                        prev_halt = bool(runtime.get('_journal_halt_open', False))
                        prev_reason = str(runtime.get('_journal_halt_reason') or '')
                        if unresolved > 0:
                            new_reason = (
                                f'journal未完成入账 {unresolved} 条'
                                f'（spread={spread_j.get("unresolved_pending", 0)}, '
                                f'strangle={strangle_j.get("unresolved_pending", 0)}, '
                                f'fill={fill_j.get("unresolved_pending", 0)}）'
                            )
                            runtime['_journal_halt_open'] = True
                            runtime['_journal_halt_reason'] = new_reason
                            if (not prev_halt) or (prev_reason != new_reason):
                                logger.warning(
                                    f'[journal] 检测到未完成入账，暂停新开: '
                                    f'{runtime["_journal_halt_reason"]}'
                                )
                        else:
                            runtime['_journal_halt_open'] = False
                            runtime['_journal_halt_reason'] = ''
                            if prev_halt:
                                logger.info('[journal] 未完成入账已清零，恢复新开判定')
                        if malformed > 0:
                            logger.warning(
                                f'[journal] 检测到 {malformed} 行损坏记录（已忽略），'
                                '请关注磁盘/断电风险'
                            )
                    except Exception as e:
                        logger.warning(f'[journal] 健康检查异常，保守暂停新开: {e}')
                        conn._runtime_state['_journal_halt_open'] = True
                        conn._runtime_state['_journal_halt_reason'] = (
                            f'journal健康检查异常: {e}'
                        )
                _update_bool_metric(
                    conn._runtime_state, 'journal_halt',
                    bool(conn._runtime_state.get('_journal_halt_open', False)),
                    now,
                )

                if (
                    now - _last_margin_check_time >= margin_recheck_interval
                    or conn._runtime_state.pop('_force_margin_check', False)
                ):
                    _last_margin_check_time = now
                    status, reason = margin_check.check_margin_status(
                        conn, config, logger, context='主循环',
                    )
                    if status == 'ok':
                        conn._runtime_state['_margin_halt_open'] = False
                        conn._runtime_state['_margin_halt_reason'] = ''
                    elif status == 'over_limit':
                        conn._runtime_state['_margin_halt_open'] = True
                        conn._runtime_state['_margin_halt_reason'] = (
                            f'{reason} (限额 {config.get("global_margin_limit", 0)})'
                        )
                        spread_logger.warning(
                            '保证金超限：暂停双策略新开；允许平仓、补A与平A，'
                            '保证金halt下 B<2*A 仅允许平A修复，不再补B'
                        )
                    else:
                        # unknown: 持仓查询多次失败，沿用上轮 halt 状态
                        # （宁可保守不切换，避免连接波动误开/误平）
                        prev_halt = bool(
                            conn._runtime_state.get('_margin_halt_open', False)
                        )
                        prev_reason = conn._runtime_state.get(
                            '_margin_halt_reason', ''
                        )
                        spread_logger.warning(
                            f'保证金检查失败（unknown），沿用上一轮风控状态'
                            f' halt={prev_halt} reason={prev_reason or "无"}'
                        )
                    try:
                        from runtime_risk_alerts import record_margin_check_result
                        record_margin_check_result(conn, config, logger, status)
                    except Exception as e:
                        logger.debug(f'[风控告警] margin: {e}')
                    _sync_strangle_open_halt(conn, ledger, str_cfg)
                _margin_halt_open = bool(
                    conn._runtime_state.get('_margin_halt_open', False)
                )

                dual = config.get('dual_strategy') or {}
                force_reconcile = conn._runtime_state.pop('_force_reconcile', False)
                if (
                    force_reconcile
                    or now - _last_reconcile_time >= reconcile_interval
                    or '_strangle_reconcile_halt' not in conn._runtime_state
                ):
                    _last_reconcile_time = now
                    halt, issues, spread_halt, spread_issues = _run_reconcile(
                        conn, ledger, spread_tradeinfo, strangle_tradeinfo,
                        spread_logger, strangle_logger, config, str_cfg, dual,
                    )
                else:
                    runtime = conn._runtime_state
                    halt = bool(runtime.get('_strangle_reconcile_halt', False))
                    issues = list(runtime.get('_strangle_reconcile_issues') or [])
                    spread_halt = bool(runtime.get('_spread_reconcile_halt', False))
                    spread_issues = list(runtime.get('_spread_reconcile_issues') or [])
                _update_bool_metric(conn._runtime_state, 'spread_halt', bool(spread_halt), now)
                _update_bool_metric(conn._runtime_state, 'strangle_halt', bool(halt), now)

                spread_open_ok = True
                if spread_halt and dual.get('pause_spread_open_on_reconcile_mismatch', True):
                    spread_open_ok = False
                    spread_logger.warning(
                        '价差对账不一致，暂停新开/再平衡（仍允许平仓）: '
                        + '; '.join(spread_issues[:3])
                    )
                if _margin_halt_open:
                    spread_open_ok = False
                if conn._runtime_state.get('_journal_halt_open', False):
                    spread_open_ok = False
                    j_reason = str(conn._runtime_state.get('_journal_halt_reason') or '')
                    last_warn_reason = str(
                        conn._runtime_state.get('_journal_spread_warn_reason') or '',
                    )
                    if j_reason and j_reason != last_warn_reason:
                        spread_logger.warning(
                            'journal存在未完成入账，暂停价差新开/再平衡（仍允许平仓）: '
                            + j_reason
                        )
                        conn._runtime_state['_journal_spread_warn_reason'] = j_reason
                else:
                    conn._runtime_state.pop('_journal_spread_warn_reason', None)

                from spread_fill_sync import count_spread_filled_open_orders
                from spread_daily_limit import resolve_spread_daily_limit

                spread_filled = 0
                spread_count_source = 'spread'
                try:
                    fc = count_spread_filled_open_orders(conn, config, timeout=2)
                    if fc is None:
                        fc = conn.get_filled_open_order_count(timeout=2)
                        spread_count_source = 'account'
                        spread_logger.warning(
                            '价差日笔数查询降级为全账户口径（含宽跨开仓），'
                            '仅新开受日限约束，平仓不受影响'
                        )
                    spread_filled, spread_open_ok = resolve_spread_daily_limit(
                        fc,
                        spread_daily_limit,
                        spread_open_ok,
                        log_warning=spread_logger.warning,
                    )
                    if fc is not None and spread_filled >= spread_daily_limit:
                        if not spread_limit_notified:
                            spread_limit_notified = True
                            spread_logger.warning(
                                f"日笔数达限 {spread_filled}/{spread_daily_limit}"
                                f'（{spread_count_source}），'
                                '仍扫描平仓/再平衡，仅禁止新开'
                            )
                except Exception as e:
                    spread_logger.error(f"成交查询异常: {e}")
                    spread_filled, spread_open_ok = resolve_spread_daily_limit(
                        None,
                        spread_daily_limit,
                        spread_open_ok,
                        log_warning=spread_logger.warning,
                    )

                strangle_buy_spent = ledger.get_daily_buy_amount()
                strangle_open_ok = strangle_buy_spent < strangle_buy_limit
                if not strangle_open_ok:
                    strangle_logger.info(
                        f"日买入 {strangle_buy_spent:.0f}/{strangle_buy_limit:.0f} 元已达上限，"
                        '仍扫描平仓/再平衡，仅禁止新开'
                    )

                begin_round_vix_cache(conn)
                round_vix_engine = wrap_vix_engine(vix_engine, conn, logger)

                for name in strategy_order:
                    if is_trading_paused():
                        break
                    if name == 'spread':
                        with strategy_logging(conn, logger, 'spread') as s_logger:
                            spread_rem = (
                                max(0, spread_daily_limit - spread_filled)
                                if spread_open_ok else 0
                            )
                            for item in spread_tradeinfo:
                                try:
                                    if auto_processor.process_symbol(
                                        conn, item, round_vix_engine, config, s_logger,
                                        remaining_limit=spread_rem,
                                    ):
                                        if not spread_open_ok:
                                            continue
                                        try:
                                            from spread_fill_sync import (
                                                count_spread_filled_open_orders,
                                            )
                                            r = count_spread_filled_open_orders(
                                                conn, config, timeout=2,
                                            )
                                            if r is None:
                                                r = conn.get_filled_open_order_count(timeout=2)
                                            if r is not None:
                                                spread_filled = r
                                                spread_rem = max(
                                                    0, spread_daily_limit - spread_filled,
                                                )
                                        except Exception as e:
                                            # 与轮初 fc=None 一致：计数不可信时禁新开，
                                            # 不用 +1 估计（避免与真实笔数漂移）。
                                            s_logger.warning(
                                                '价差日笔数刷新失败，本轮余量置 0 '
                                                f'保守禁新开: {e}'
                                            )
                                            spread_rem = 0
                                            spread_open_ok = False
                                except Exception as e:
                                    s_logger.error(f"[{item['future']}] {e}", exc_info=True)
                    elif name == 'strangle':
                        with strategy_logging(conn, logger, 'strangle') as s_logger:
                            strangle_count = len(strangle_tradeinfo)
                            strangle_t0 = time.time()
                            strangle_acted = 0
                            s_logger.info(f"开始扫描 {strangle_count} 个品种")
                            for item in strangle_tradeinfo:
                                try:
                                    if process_strangle_symbol(
                                        conn, item, round_vix_engine, config,
                                        s_logger,
                                        ledger, str_executor, circuit_breaker,
                                    ):
                                        strangle_acted += 1
                                except Exception as e:
                                    s_logger.error(f"[{item['future']}] {e}", exc_info=True)
                            elapsed = time.time() - strangle_t0
                            s_logger.info(
                                f"扫描完成 ({elapsed:.1f}s)，"
                                f"{strangle_acted}/{strangle_count} 个品种有操作"
                            )

                unmatched = ledger.list_unmatched_legs()
                with strategy_logging(conn, logger, 'strangle') as s_logger:
                    if unmatched:
                        s_logger.info(f"再平衡：{len(unmatched)} 条未配对腿")
                    # 对账 halt 与保证金 halt 都需要降级为 close-only：
                    #   - 保证金 halt：账本可信，但禁止增风险（开仓类 awaiting_phase2）。
                    #   - 对账 halt：账本不可信，autostraggle.run_rebalance 不读
                    #     ledger.is_open_halted()（已验证），会照常跑 awaiting_phase2 的
                    #     第二腿开仓，违反"对账 halt = close-only"约定。
                    # close_chp_pending 必须继续跑，否则残留单腿 = 裸期权。
                    strangle_reconcile_halt = bool(
                        conn._runtime_state.get('_strangle_reconcile_halt', False)
                    )
                    if not _margin_halt_open and not strangle_reconcile_halt:
                        str_executor.run_rebalance(tradeinfo_by_key)
                    else:
                        from strangle_rebalance_close_only import (
                            CLOSE_KINDS,
                            run_close_only_rebalance,
                        )
                        if _margin_halt_open and strangle_reconcile_halt:
                            halt_reason = '保证金超限+对账 halt'
                        elif _margin_halt_open:
                            halt_reason = '保证金超限'
                        else:
                            halt_reason = '对账 halt'
                        close_pending = sum(
                            1 for it in unmatched
                            if it.get('kind') in CLOSE_KINDS
                        )
                        if close_pending:
                            s_logger.info(
                                f'{halt_reason}：仅处理 {close_pending} 条平仓类未配对腿'
                                f'（开仓类暂停）'
                            )
                            try:
                                handled = run_close_only_rebalance(
                                    str_executor, ledger, tradeinfo_by_key,
                                )
                                if handled:
                                    s_logger.info(
                                        f'{halt_reason}期间平仓类再平衡处理 {handled} 条'
                                    )
                            except Exception as e:
                                s_logger.error(
                                    f'{halt_reason}期间平仓类再平衡异常: {e}',
                                    exc_info=True,
                                )
                        else:
                            s_logger.info(
                                f'{halt_reason}，跳过宽跨再平衡（无平仓类待处理）'
                            )
                    try:
                        from strangle_unmatched_watchdog import (
                            check_unmatched_health,
                        )
                        check_unmatched_health(conn, ledger, config, s_logger)
                    except Exception as e:
                        s_logger.debug(f'[宽跨守护] watchdog 异常: {e}')
                    try:
                        from runtime_risk_alerts import (
                            notify_reconcile_halt_open_unmatched,
                        )
                        notify_reconcile_halt_open_unmatched(
                            conn, ledger, config, s_logger,
                        )
                    except Exception as e:
                        s_logger.debug(f'[风控告警] reconcile halt open: {e}')

                try:
                    snapshot = build_strategy_status_snapshot(
                        conn=conn,
                        ledger=ledger,
                        config=config,
                        spread_tradeinfo=spread_tradeinfo,
                        strangle_tradeinfo=strangle_tradeinfo,
                        vix_engine=round_vix_engine,
                        spread_halt=spread_halt,
                        strangle_reconcile_halt=halt,
                        margin_halt_open=_margin_halt_open,
                        spread_open_ok=spread_open_ok,
                        spread_filled=spread_filled,
                        spread_daily_limit=spread_daily_limit,
                        strangle_buy_spent=strangle_buy_spent,
                        strangle_buy_limit=strangle_buy_limit,
                        feishu_paused=is_trading_paused(),
                        logger=logger,
                        journal_halt_open=bool(
                            conn._runtime_state.get('_journal_halt_open', False),
                        ),
                        journal_halt_reason=str(
                            conn._runtime_state.get('_journal_halt_reason', '') or '',
                        ),
                    )
                    conn._runtime_state['_strategy_status_snapshot'] = snapshot
                except Exception as e:
                    logger.debug(f'[状态快照] 构建失败: {e}')

                round_elapsed = time.time() - round_t0
                logger.debug(
                    f'[主循环] 本轮 {round_elapsed:.1f}s | '
                    f'价差日限 {spread_filled}/{spread_daily_limit} | '
                    f'宽跨买入 {strangle_buy_spent:.0f}/{strangle_buy_limit:.0f} | '
                    f'对账 halt S={spread_halt} T={halt}'
                )
                _consecutive_loop_errors = 0
                time.sleep(loop_interval)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                _consecutive_loop_errors += 1
                conn._runtime_state['_metric_loop_error_count'] = (
                    int(conn._runtime_state.get('_metric_loop_error_count', 0) or 0) + 1
                )
                logger.error(
                    f"[主循环] ({_consecutive_loop_errors}/{_max_loop_errors or '∞'}) {e}",
                    exc_info=True,
                )
                if _max_loop_errors > 0 and _consecutive_loop_errors >= _max_loop_errors:
                    logger.error(
                        f'[主循环] 连续 {_consecutive_loop_errors} 次异常，'
                        '触发进程级重启'
                    )
                    raise
                time.sleep(loop_interval)
    except KeyboardInterrupt:
        logger.info("用户中断")
    finally:
        stop_command_receiver()
        try:
            conn.cancel_all_pending_orders(
                timeout=conn.config.get('CANCEL_ALL_TIMEOUT', 5))
        except Exception as e:
            logger.error(f"退出撤单: {e}")
