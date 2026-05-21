"""启动 banner（无 autotrade 依赖，供 merged_main 与 unit 测试共用）。"""

from __future__ import annotations


def log_startup_banner(config, spread_info, strangle_info, logger) -> None:
    logger.info('=' * 80)
    logger.info('AutoCTP 单进程双策略')
    logger.info(f'价差品种: {len(spread_info)}, 宽跨品种: {len(strangle_info)}')
    spread_map = {it['future'].lower(): it['month'] for it in spread_info}
    strangle_map = {it['future'].lower(): it['month'] for it in strangle_info}

    overlap = sorted(set(spread_map.keys()) & set(strangle_map.keys()))
    if overlap:
        logger.warning(
            f'⚠ 同覆盖品种 {len(overlap)} 个 (依赖 OrderRef 段位区分): '
            f"{', '.join(s.upper() for s in overlap)}"
        )
        for sym in overlap:
            sm = spread_map[sym]
            gm = strangle_map[sym]
            if sm != gm:
                logger.info(f'  {sym.upper()}: 价差 month={sm}, 宽跨 month={gm}')
            else:
                logger.info(f'  {sym.upper()}: 同 month={sm}')

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

    dual = config.get('dual_strategy') or {}
    margin_limit = config.get('global_margin_limit', 100000)
    margin_recheck = config.get('margin_recheck_interval_sec', 0)
    reconcile_grace = dual.get('reconcile_grace_after_derive_sec', 0)
    journal_shards = dual.get('journal_daily_shards', False)
    logger.info(
        f'风控: global_margin_limit={margin_limit} 元 (0=禁用), '
        f'margin_recheck={margin_recheck}s, '
        f'reconcile_grace_after_derive={reconcile_grace}s, '
        f'journal_daily_shards={journal_shards}'
    )

    try:
        from auto_runtime_profile import format_environment_banner
        logger.info(format_environment_banner(config))
    except Exception as e:
        logger.warning('环境 banner 输出失败: %s', e, exc_info=True)
    logger.info('=' * 80)
