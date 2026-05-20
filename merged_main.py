"""
AutoCTP — 单进程双策略（价差 + 宽跨）

不修改 D:\\autotrade、D:\\autostraggle，仅引用其代码。
"""

import os
import sys
import time

import ctp_bootstrap  # noqa: F401 — 注入 autotrade / autostraggle 路径

from auto_vix import VIXEngine
from auto_initializer import initialize_connection, prepare_trading_environment

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
    for it in strangle_info:
        sym = it['future'].lower()
        sm = spread_map.get(sym)
        if sm and sm != it['month']:
            logger.info(
                f"  {it['future']}: 价差 month={sm}, 宽跨 month={it['month']}"
            )
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
    try:
        from auto_runtime_profile import format_environment_banner
        logger.info(format_environment_banner(config))
    except Exception:
        pass
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


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, '').strip().lower() in ('1', 'yes', 'true', 'y')


def _resolve_manual_start(config: dict) -> bool:
    """人工启动：显式 --manual / AUTOCTP_MANUAL；否则非进程内自动重启视为人工。"""
    if _env_truthy('AUTOCTP_MANUAL') or '--manual' in sys.argv:
        return True
    if _env_truthy('AUTOCTP_AUTO_RESTART') or '--auto-restart' in sys.argv:
        return False
    if config.get('_auto_restart'):
        return False
    return True


def _prepare_env(conn, combined, config, logger):
    retry = config.get('env_retry_interval', 60)
    while True:
        _, ready = prepare_trading_environment(conn, combined, config, logger)
        if ready:
            return ready
        logger.warning(f"无期货价格，{retry}s 后重试")
        time.sleep(retry)


def _margin_ok(conn, config, logger) -> bool:
    limit = config.get('global_margin_limit', 0)
    if limit <= 0:
        return True
    retry = config.get('margin_retry_interval', 30)
    max_attempts = config.get('margin_check_max_attempts', 3)
    from auto_risk import sum_positions_margin_for_limit

    for attempt in range(max_attempts):
        pos = conn.query_positions_sync(timeout=10)
        if pos is None:
            logger.warning(
                f"保证金检查: 持仓查询失败 ({attempt + 1}/{max_attempts})"
            )
            if attempt + 1 < max_attempts:
                time.sleep(retry)
            continue
        total, _ = sum_positions_margin_for_limit(conn, pos, config)
        if total > limit:
            logger.error(f"保证金超限 {total:.2f} > {limit:.2f}")
            return False
        return True

    logger.error("保证金检查: 持仓查询多次失败，拒绝启动")
    return False


def main():
    try:
        config = load_merged_config()
        logger = setup_merged_logger(config)
    except Exception as e:
        print(f"启动失败: {e}")
        sys.exit(1)

    str_cfg = config.get('strangle', {})
    ledger = StrangleLedger(str_cfg['ledger_path'])
    restart_delay = config.get('restart_delay', 30)

    while True:
        conn = None
        try:
            config['_manual_start'] = _resolve_manual_start(config)

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
            logger.error(f"异常，{restart_delay}s 后重启: {e}", exc_info=True)
            time.sleep(restart_delay)
        finally:
            if conn:
                try:
                    conn.release()
                except Exception:
                    pass


if __name__ == '__main__':
    main()
