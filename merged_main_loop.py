"""单进程双策略主循环（价差 + 宽跨）。"""

import time

from auto_initializer import manage_future_price_readiness
from auto_processor import process_symbol

from merged_strategy_logger import strategy_logger, strategy_logging
from merged_vix_cache import begin_round_vix_cache, wrap_vix_engine


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
    from straggle_reconcile import reconcile_strangle_positions
    from strangle_reconcile_dual import reconcile_strangle_positions_dual
    from auto_circuit_breaker import CircuitBreaker
    from auto_health_check import HealthChecker
    from auto_feishu_command import (
        start_command_receiver,
        is_trading_paused,
        stop_command_receiver,
    )
    from auto_scheduled_reconnect import check_scheduled_full_recovery
    from auto_scheduled_pause import sync_connection_suspend_state

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

    start_command_receiver(config)
    loop_interval = config.get('loop_interval', 10)
    spread_daily_limit = config.get('daily_trade_limit', 100)
    strangle_buy_limit = float(str_cfg.get('daily_buy_limit_yuan', 300000))
    fp_interval = config.get('future_price_status_interval', 60)
    spread_limit_notified = False
    _last_unhealthy_alert_time = 0.0
    _health_alert_cooldown = config.get('health_alert_cooldown', 300)

    logger.info("=" * 60)
    logger.info("AutoCTP 双策略主循环")
    logger.info(f"价差 {len(spread_tradeinfo)} 品种, 宽跨 {len(strangle_tradeinfo)} 品种")
    logger.info(f"顺序: {strategy_order}, 全局 1 在途")
    logger.info("=" * 60)

    try:
        while True:
            try:
                sync_connection_suspend_state(conn, config, logger)
                if check_scheduled_full_recovery(conn, config, logger):
                    time.sleep(loop_interval)
                    continue

                health_report = health_checker.check_now(force=False)
                if health_report and not health_report.get('healthy'):
                    issues = '; '.join(health_report.get('issues', []))
                    logger.warning(f"[健康] {issues}")
                    is_reconnecting = (
                        conn._td_disconnect_notified or conn._md_disconnect_notified
                    )
                    if not is_reconnecting:
                        now = time.time()
                        if now - _last_unhealthy_alert_time >= _health_alert_cooldown:
                            _last_unhealthy_alert_time = now
                            try:
                                from auto_feishu import send_feishu_message
                                summary = health_checker.get_summary()
                                send_feishu_message(
                                    f"🔴 **系统健康检查异常**\n\n{summary}",
                                    config=config,
                                )
                            except Exception as e:
                                logger.debug(f"健康检查飞书通知失败: {e}")

                if conn._reconnect_quarantine or not conn.td_logined or not conn.md_logined:
                    from auto_reconnect_recovery import check_quarantine_watchdog
                    from auto_scheduled_pause import is_connection_suspended
                    conn._runtime_state['_fill_ledger_replay_pending'] = True
                    if conn._reconnect_quarantine and not is_connection_suspended(config):
                        check_quarantine_watchdog(conn, config, logger)
                    if conn._reconnect_quarantine:
                        logger.info("[主循环] 处于重连隔离期，等待撤单+持仓校准完成...")
                    else:
                        logger.info(
                            "[主循环] 交易/行情未全部登录 "
                            f"(td={conn.td_logined}, md={conn.md_logined})，跳过本轮"
                        )
                    time.sleep(loop_interval)
                    continue

                if conn._runtime_state.pop('_fill_ledger_replay_pending', False):
                    try:
                        from fill_ledger import sync_fill_ledger_from_trades
                        sync_fill_ledger_from_trades(conn, config, logger)
                    except Exception as e:
                        logger.debug(f'[FillLedger] post-reconnect replay: {e}')

                if is_trading_paused():
                    with conn._executor_lock:
                        ex = conn._active_executor
                        if ex:
                            try:
                                ex.stop_all_threads.set()
                                ex.cleanup()
                            except Exception:
                                pass
                            conn._active_executor = None
                    time.sleep(loop_interval)
                    continue

                manage_future_price_readiness(
                    conn, combined_tradeinfo, logger, conn._runtime_state, fp_interval,
                )

                str_symbols = {it['future'].lower() for it in strangle_tradeinfo}
                dual = config.get('dual_strategy') or {}
                if dual.get('exclude_spread_from_strangle_reconcile', True):
                    halt, issues = reconcile_strangle_positions_dual(
                        conn, ledger, str_symbols, spread_tradeinfo,
                        strangle_logger, config=config,
                    )
                else:
                    halt, issues = reconcile_strangle_positions(
                        conn, ledger, str_symbols, strangle_logger, config=config,
                    )
                if str_cfg.get('pause_open_on_reconcile_mismatch', True):
                    ledger.set_open_halt(
                        halt,
                        '; '.join(issues[:5]) if halt else '',
                    )

                spread_halt = False
                spread_issues: list = []
                if dual.get('spread_execution_from_ledger', True):
                    try:
                        from spread_reconcile import reconcile_spread_positions
                        from spread_ledger_execution import set_spread_open_halt

                        spread_halt, spread_issues = reconcile_spread_positions(
                            conn, spread_tradeinfo, spread_logger, config=config,
                        )
                        if dual.get('pause_spread_open_on_reconcile_mismatch', True):
                            set_spread_open_halt(
                                conn,
                                spread_halt,
                                '; '.join(spread_issues[:5]) if spread_halt else '',
                            )
                    except Exception as e:
                        spread_logger.warning(f'[spread-reconcile] {e}')
                        if dual.get('pause_spread_open_on_reconcile_mismatch', True):
                            from spread_ledger_execution import set_spread_open_halt
                            set_spread_open_halt(conn, True, str(e))
                            spread_halt = True

                spread_open_ok = True
                if spread_halt and dual.get('pause_spread_open_on_reconcile_mismatch', True):
                    spread_open_ok = False
                    spread_logger.warning(
                        '价差对账不一致，暂停新开/再平衡（仍允许平仓）: '
                        + '; '.join(spread_issues[:3])
                    )

                spread_filled = 0
                spread_count_source = 'spread'
                try:
                    from spread_fill_sync import count_spread_filled_open_orders
                    fc = count_spread_filled_open_orders(conn, config, timeout=2)
                    if fc is None:
                        fc = conn.get_filled_open_order_count(timeout=2)
                        spread_count_source = 'account'
                        spread_logger.warning(
                            '价差日笔数查询降级为全账户口径（含宽跨开仓），'
                            '仅新开受日限约束，平仓不受影响'
                        )
                    if fc is None:
                        conn.cancel_all_pending_orders()
                        time.sleep(loop_interval)
                        continue
                    spread_filled = fc
                    if spread_open_ok:
                        spread_open_ok = fc < spread_daily_limit
                    if not spread_open_ok and not spread_limit_notified:
                        spread_limit_notified = True
                        spread_logger.warning(
                            f"日笔数达限 {fc}/{spread_daily_limit}（{spread_count_source}），"
                            '仍扫描平仓/再平衡，仅禁止新开'
                        )
                except Exception as e:
                    spread_logger.error(f"成交查询异常: {e}")
                    time.sleep(loop_interval)
                    continue

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
                                    if process_symbol(
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
                                        except Exception:
                                            spread_filled += 1
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
                                        conn, item, vix_engine, config, s_logger,
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
                    str_executor.run_rebalance(tradeinfo_by_key)
                time.sleep(loop_interval)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                logger.error(f"[主循环] {e}", exc_info=True)
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
