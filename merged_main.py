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
from merged_config import load_merged_config, setup_merged_logger, prepare_merged_connection
from merged_startup_checks import apply_startup_margin, audit_target_months
from merged_tradeinfo import load_dual_tradeinfo
from merged_startup_ack import require_startup_position_ack
from merged_main_loop import run_merged_main_loop
from merged_banner import log_startup_banner
from straggle_ledger import StrangleLedger


def _log_banner(config, spread_info, strangle_info, logger):
    """向后兼容别名。"""
    log_startup_banner(config, spread_info, strangle_info, logger)


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
    from order_whitelist_guard import (
        install_send_order_month_guard,
        get_install_error as _whitelist_install_error,
    )
    from ctp_heartbeat_guard import install_heartbeat_warning
    from ctp_recovery_patch import install_recovery_patch
    from health_check_patch import install_health_check_patch
    install_atomic_save()
    # 发单月白名单守卫是核心邻月错单防护；安装失败必须显式告警，
    # 不能像旧实现那样静默 return。
    whitelist_ok = install_send_order_month_guard()
    if not whitelist_ok:
        reason = _whitelist_install_error() or '未知原因'
        msg = (
            f'发单月白名单守卫未安装：{reason}。'
            '邻月错单将仅靠 autotrade 品种级检查；'
            '建议检查 autotrade 版本与 sys.path 后再启动。'
        )
        logger.error('[启动自检] %s', msg)
        try:
            from auto_feishu import send_feishu_message
            send_feishu_message(
                f'⚠️ **AutoCTP 启动自检告警**\n\n{msg}',
                config=config,
            )
        except Exception as notify_err:
            logger.warning(
                '守卫未安装飞书通知失败: %s', notify_err, exc_info=True,
            )
        if config.get('fail_fast_on_guard_install', False):
            logger.error('[启动自检] fail_fast_on_guard_install=true，拒绝启动')
            sys.exit(4)
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
            audit_target_months(conn, config, logger, spread_info, strangle_info)
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
            from spread_claims_guard import purge_invalid_spread_claims
            purge_invalid_spread_claims(
                config, conn, spread_info, store=spread_store, logger=logger,
            )
            sync_spread_leg_claims(spread_store, config, logger=logger)
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

            if not apply_startup_margin(conn, config, logger, ledger, str_cfg):
                conn.release()
                config['_auto_restart'] = True
                time.sleep(60)
                continue

            if (
                conn._runtime_state.get('_margin_halt_open')
                and str_cfg.get('pause_open_on_reconcile_mismatch', True)
            ):
                from merged_main_loop import _sync_strangle_open_halt
                _sync_strangle_open_halt(conn, ledger, str_cfg)

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
                # 旧实现只打日志后继续 while True；与日志/飞书宣称的"进程退出"
                # 不一致，会无限重启。这里抛 SystemExit 让 finally 释放 conn
                # 后真正退出（外层 KeyboardInterrupt/Exception 不捕获 SystemExit）。
                sys.exit(3)
            time.sleep(delay)
        finally:
            if conn:
                try:
                    conn.release()
                except Exception as release_err:
                    logger.warning('连接释放异常: %s', release_err, exc_info=True)


if __name__ == '__main__':
    main()
