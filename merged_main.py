"""
AutoCTP — 单进程双策略（价差 + 宽跨）

不修改 D:\\autotrade、D:\\autostraggle，仅引用其代码。
"""

import sys
import time

import ctp_bootstrap  # noqa: F401 — 注入 autotrade / autostraggle 路径

from auto_vix import VIXEngine
from auto_initializer import initialize_connection, prepare_trading_environment

from env_utils import resolve_manual_start
from margin_check import check_margin_status
from merged_config import load_merged_config, setup_merged_logger, prepare_merged_connection
from merged_tradeinfo import load_dual_tradeinfo
from merged_startup_ack import require_startup_position_ack
from merged_main_loop import run_merged_main_loop
from straggle_ledger import StrangleLedger


def _log_banner(config, spread_info, strangle_info, logger):
    logger.info("=" * 80)
    logger.info("AutoCTP 单进程双策略")
    logger.info(f"价差品种: {len(spread_info)}, 宽跨品种: {len(strangle_info)}")
    spread_map = {it['future'].lower(): it['month'] for it in spread_info}
    strangle_map = {it['future'].lower(): it['month'] for it in strangle_info}

    overlap = sorted(set(spread_map.keys()) & set(strangle_map.keys()))
    if overlap:
        # 同时被两策略覆盖的品种：成交后只能靠 OrderRef 段位区分，CSV/账本
        # 完全靠 OrderRef 分桶；提示操作员留意冲突风险。
        logger.warning(
            f"⚠ 同覆盖品种 {len(overlap)} 个 (依赖 OrderRef 段位区分): "
            f"{', '.join(s.upper() for s in overlap)}"
        )
        for sym in overlap:
            sm = spread_map[sym]
            gm = strangle_map[sym]
            if sm != gm:
                logger.info(
                    f"  {sym.upper()}: 价差 month={sm}, 宽跨 month={gm}"
                )
            else:
                logger.info(f"  {sym.upper()}: 同 month={sm}")

    logger.info(
        f"价差: VIX > {config.get('VIX_TRIGGER_MULTIPLIER', 1)}×vol_basis×100, "
        f"日笔数≤{config.get('daily_trade_limit', 100)}"
    )
    sc = config.get('strangle', {})
    logger.info(
        f"宽跨: VIX < vol_basis×{sc.get('benchmark_multiplier', 0.8)}×100, "
        f"日买入≤{sc.get('daily_buy_limit_yuan', 300000):.0f} 元"
    )
    logger.info(f"循环间隔: {config.get('loop_interval', 10)} 秒")

    # P8: 补全关键风控参数 banner
    dual = config.get('dual_strategy') or {}
    margin_limit = config.get('global_margin_limit', 100000)
    margin_recheck = config.get('margin_recheck_interval_sec', 0)
    reconcile_grace = dual.get('reconcile_grace_after_derive_sec', 0)
    journal_shards = dual.get('journal_daily_shards', False)
    logger.info(
        f"风控: global_margin_limit={margin_limit} 元 (0=禁用), "
        f"margin_recheck={margin_recheck}s, "
        f"reconcile_grace_after_derive={reconcile_grace}s, "
        f"journal_daily_shards={journal_shards}"
    )

    try:
        from auto_runtime_profile import format_environment_banner
        logger.info(format_environment_banner(config))
    except Exception as e:
        logger.warning('环境 banner 输出失败: %s', e, exc_info=True)
    logger.info("=" * 80)


def _init_conn(config, logger, combined):
    from auto_scheduled_pause import wait_while_connection_suspended

    retry = config.get('init_retry_interval', 60)
    max_iv = config.get('init_retry_max_interval', 300)
    poll = config.get('session_pause_poll_sec', 30)
    n = 0
    while True:
        wait_while_connection_suspended(config, logger, poll_sec=poll)
        n += 1
        try:
            logger.info(f"初始化 CTP (第 {n} 次)...")
            conn = initialize_connection(config, logger, combined)
            prepare_merged_connection(conn, config)
            return conn
        except Exception as e:
            iv = min(retry * (2 ** min(n - 1, 4)), max_iv)
            logger.warning(f"初始化失败: {e}，{iv}s 后重试")
            time.sleep(iv)


def _prepare_env(conn, combined, config, logger):
    retry = config.get('env_retry_interval', 60)
    while True:
        _, ready = prepare_trading_environment(conn, combined, config, logger)
        if ready:
            return ready
        logger.warning(f"无期货价格，{retry}s 后重试")
        time.sleep(retry)


def _margin_ok(conn, config, logger) -> bool:
    status, reason = check_margin_status(conn, config, logger, context='启动')
    if status == 'ok':
        return True
    if status == 'over_limit':
        return False
    logger.warning(
        f'启动保证金检查无法判定（{reason}），暂不拦截启动；'
        '主循环将周期性复检'
    )
    return True


def main():
    try:
        config = load_merged_config()
        logger = setup_merged_logger(config)
    except Exception as e:
        print(f"启动失败: {e}")
        sys.exit(1)

    # P6: 单实例守护——双进程会破坏 fill_ledger / journal 的 in-process 去重，
    # 这里在加载完 config 之后、动任何 CTP/账本前先抢锁；冲突直接退出。
    # 可通过 config['singleton_pid_path'] 自定义路径；默认 data/autoctp.pid。
    if not config.get('disable_singleton_guard'):
        from process_guard import acquire_singleton, AlreadyRunningError
        try:
            acquire_singleton(
                pid_path=config.get('singleton_pid_path'),
                logger=logger,
            )
        except AlreadyRunningError as e:
            logger.error(str(e))
            try:
                from auto_feishu import send_feishu_message
                send_feishu_message(
                    f'🔴 **AutoCTP 启动被拒绝**\n\n{e}',
                    config=config,
                )
            except Exception as e:
                logger.warning('启动拒绝飞书通知发送失败: %s', e, exc_info=True)
            sys.exit(2)

    from strangle_ledger_atomic import install_atomic_save
    from order_whitelist_guard import install_send_order_month_guard
    from ctp_heartbeat_guard import install_heartbeat_warning
    from ctp_recovery_patch import install_recovery_patch
    from health_check_patch import install_health_check_patch
    install_atomic_save()
    install_send_order_month_guard()
    install_heartbeat_warning()
    install_recovery_patch()
    install_health_check_patch()

    str_cfg = config.get('strangle', {})
    ledger = StrangleLedger(str_cfg['ledger_path'])
    restart_delay = config.get('restart_delay', 30)
    max_restart_delay = config.get('max_restart_delay', 600)
    max_restart_attempts = int(config.get('max_restart_attempts', 0) or 0)

    while True:
        conn = None
        try:
            config['_manual_start'] = resolve_manual_start(config)

            spread_info, strangle_info, combined = load_dual_tradeinfo(config)
            config['spread_tradeinfo'] = spread_info
            config['strangle_tradeinfo'] = strangle_info
            _log_banner(config, spread_info, strangle_info, logger)

            from auto_feishu import get_notifier
            from trade_feishu_notify import install_unified_trade_feishu
            from spread_ledger_execution import install_spread_ledger_execution

            get_notifier(config)
            install_unified_trade_feishu(config)
            install_spread_ledger_execution(config)

            conn = _init_conn(config, logger, combined)
            active = _prepare_env(conn, combined, config, logger)
            from auto_utils import log_startup_min_ticks
            log_startup_min_ticks(conn, combined, logger)

            from import_strangle_positions import sync_strangle_leg_claims
            from import_spread_positions import sync_spread_leg_claims
            from spread_ledger import SpreadLegStore
            from strangle_fill_sync import sync_csv_from_strangle_trades, wire_strangle_trade_runtime
            from spread_fill_sync import sync_csv_from_spread_trades, wire_spread_trade_runtime
            from fill_ledger import wire_fill_ledger, sync_fill_ledger_from_trades

            spread_store = SpreadLegStore()
            wire_strangle_trade_runtime(conn, ledger)
            wire_spread_trade_runtime(conn, spread_store)
            wire_fill_ledger(conn)
            sync_strangle_leg_claims(ledger, config, logger=logger)
            sync_spread_leg_claims(spread_store, config, logger=logger)

            from strangle_close_only_holdback import recover_holdback_into_ledger
            recover_holdback_into_ledger(ledger, logger=logger)
            if config.get('strangle', {}).get('auto_sync_positions_csv', True):
                sync_csv_from_strangle_trades(conn, ledger, config, logger)
            dual = config.get('dual_strategy') or {}
            if dual.get('auto_sync_spread_positions_csv', True):
                sync_csv_from_spread_trades(conn, spread_store, config, logger)
            sync_fill_ledger_from_trades(conn, config, logger)

            if not require_startup_position_ack(config, logger, ledger, conn):
                time.sleep(1)
                conn = None
                if config.get('_startup_ack_retry', True):
                    logger.error("等待持仓确认后重新启动...")
                    time.sleep(60)
                    continue
                logger.info("已取消启动")
                break

            if not _margin_ok(conn, config, logger):
                conn.release()
                config['_auto_restart'] = True
                time.sleep(60)
                continue

            config['_restart_failures'] = 0
            vix = VIXEngine(config)
            from auto_health_check import HealthChecker
            health = HealthChecker(conn, config, logger)

            run_merged_main_loop(
                conn, spread_info, strangle_info, active,
                vix, config, logger, ledger, health_checker=health,
            )
            break
        except KeyboardInterrupt:
            logger.info("用户中断")
            break
        except Exception as e:
            config['_auto_restart'] = True
            failures = int(config.get('_restart_failures', 0)) + 1
            config['_restart_failures'] = failures
            delay = min(
                restart_delay * (2 ** min(failures - 1, 5)),
                max_restart_delay,
            )
            logger.error(
                f"异常 (连续 {failures} 次)，{delay:.0f}s 后重启: {e}",
                exc_info=True,
            )
            if max_restart_attempts > 0 and failures >= max_restart_attempts:
                logger.error(
                    f"已达 max_restart_attempts={max_restart_attempts}，进程退出"
                )
                try:
                    from auto_feishu import send_feishu_message
                    send_feishu_message(
                        f"🔴 **AutoCTP 连续异常退出**\n\n"
                        f"连续失败 {failures} 次，最后错误: {e}",
                        config=config,
                    )
                except Exception as notify_err:
                    logger.warning(
                        '连续异常退出飞书通知发送失败: %s', notify_err, exc_info=True,
                    )
            time.sleep(delay)
        finally:
            if conn:
                try:
                    conn.release()
                except Exception as release_err:
                    logger.warning('连接释放异常: %s', release_err, exc_info=True)


if __name__ == '__main__':
    main()
