"""配置：autotrade 统一配置 + autoctp 本地 merged_config.yaml。"""

import os
from typing import Any, Dict, Tuple

STRANGLE_DEFAULTS = {
    'enabled': True,
    'daily_buy_limit_yuan': 300000,
    'max_symbols': 10,
    'min_days_to_expiry': 60,
    'close_days_to_expiry': 30,
    'breakout_buffer_pct': 0.01,
    'benchmark_multiplier': 0.8,
    'post_close_cooldown_sec': 300,
    'phase1_timeout': 180,
    'phase2_timeout': 15,
    'phase2_max_retries': 5,
    'phase1_spread_pct': 0.25,
    'ledger_path': 'data/ledger_strangle.json',
    'order_ref_min': 500000,
    'pause_open_on_reconcile_mismatch': True,
    'rebalance_max_per_round': 12,
}


def _project_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def _merge_dict(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = base.copy()
    for key, value in override.items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _merge_dict(out[key], value)
        else:
            out[key] = value
    return out


DUAL_STRATEGY_DEFAULTS = {
    'strategy_order': ['spread', 'strangle'],
    'spread_order_ref_max': 499999,

    'journal_daily_shards': True,
    'journal_retain_days': 14,
    'reconcile_interval_sec': 60,

    'tradeinfo_path': 'tradeinfo',
    'spread_sheet': 'spread',
    'strangle_sheet': 'strangle',
    'spread_csv': 'tradeinfo/spread.csv',
    'strangle_csv': 'tradeinfo/strangle.csv',

    'spread_positions_csv': 'data/spread_positions.csv',
    'strangle_positions_csv': 'data/strangle_positions.csv',
    'spread_trade_journal': 'data/spread_trade_journal.jsonl',
    'strangle_trade_journal': 'data/strangle_trade_journal.jsonl',
    'fill_ledger_csv': 'data/fill_ledger.csv',
    'fill_ledger_journal': 'data/fill_ledger_journal.jsonl',

    'use_spread_leg_claims': True,
    'spread_execution_from_ledger': True,
    'spread_close_from_ledger': True,
    'exclude_spread_from_strangle_reconcile': True,
    'exclude_strangle_from_spread_positions': True,
    'exclude_strangle_from_spread_reconcile': True,
    'pause_spread_open_on_reconcile_mismatch': True,
    'spread_fill_require_tradeinfo_match': True,
    'spread_fill_skip_strangle_owned_instruments': True,
    'spread_derive_require_tradeinfo_match': True,
    'spread_purge_invalid_claims_on_startup': True,
    'spread_reconcile_fallback_heuristic': False,
    'auto_sync_spread_positions_csv': True,

    'unified_fill_feishu': True,
    'fill_feishu_enabled': True,

    'require_startup_ack': True,
    # 7×24：人工冷启动仍交互确认；进程内 _auto_restart 才凭 ack 文件跳过。
    'startup_ack_each_run': False,
    'startup_ack_interactive': True,
    'startup_ack_use_gui': True,
    'startup_ack_prefer_gui': True,
    'startup_ack_force_terminal': False,
    'startup_ack_persist': True,
    'startup_ack_require_today': False,
    'startup_ack_file': 'data/position_startup_ack.txt',
    # 确认时记录 spread/strangle CSV 与宽跨 ledger 指纹；改文件后自动重启将拒用旧 ack
    'startup_ack_track_ledger_files': True,
    'startup_ack_tracked_files': [],
    'external_positions_ack_file': 'data/external_positions_ack.json',
    'external_ack_persist': True,
    'external_ack_require_today': False,
    'external_ack_strict_on_restore': True,
    'allow_start_on_reconcile_mismatch': False,
}

# 未在 merged_config.yaml 显式设置时采用的 AutoCTP 顶层默认值（面向 7×24 无人值守）
MERGED_TOP_LEVEL_DEFAULTS = {
    'global_margin_limit': 100000,
    'main_loop_max_consecutive_errors': 10,
    # 长跑磁盘治理（7×24）：清理过期 journal 分片、轮转 fill_ledger、保留日志天数。
    'housekeeping_enabled': True,
    'housekeeping_interval_sec': 21600,
    'log_retain_days': 30,
    'fill_ledger_rotate_enabled': True,
    'fill_ledger_max_mb': 50,
    'fill_ledger_archive_keep': 10,
    # 周末非交易抑制（仅双休日；法定节假日不处理，当交易日）。周六仅在此时刻
    # 之后才算周末，避开周五夜盘跨零点到周六凌晨。
    'weekend_pause_enabled': True,
    'weekend_pause_saturday_from_hour': 5,
    # 单轮看门狗：一轮耗时超阈值告警（CTP 查询串行变慢的可观测性）。
    'round_slow_warn_sec': 30,
    'slow_round_alert_cooldown_sec': 300,
    # 保证金连续 unknown（持仓查询失败）达到该次数后，即使上一轮非 halt 也
    # 保守暂停新开，消除"长期查询失败 → 真实超限无法发现仍可新开"的盲区。
    # 0 表示禁用该升级（仅沿用上轮状态，回到旧行为）。
    'margin_unknown_halt_after': 3,
    'fail_fast_on_guard_install': True,
    'fail_fast_on_empty_target_months': True,
    'block_start_without_margin_limit': True,
    'compat_lock_path': 'docs/compat_lock.yaml',
    'compat_lock_enforce': False,
    'compat_lock_warn_dirty': True,
}


def _validate_merged_config(config: dict) -> Tuple[list, list]:
    errors: list = []
    warnings: list = []
    dual = config.get('dual_strategy') or {}
    order = dual.get('strategy_order', ['spread', 'strangle'])
    if not isinstance(order, list) or not order:
        errors.append('dual_strategy.strategy_order 必须为非空列表')
    else:
        bad = [x for x in order if x not in ('spread', 'strangle')]
        if bad:
            errors.append(f'dual_strategy.strategy_order 含非法策略: {bad}')

    spread_max = int(dual.get('spread_order_ref_max', 499999))
    str_min = int((config.get('strangle') or {}).get('order_ref_min', 500000))
    if spread_max >= str_min:
        errors.append(
            f'OrderRef 分段冲突: spread_order_ref_max={spread_max} '
            f'>= strangle.order_ref_min={str_min}'
        )

    reconcile_iv = float(dual.get('reconcile_interval_sec', 60))
    if reconcile_iv < 0:
        errors.append('dual_strategy.reconcile_interval_sec 不能为负')

    if 'global_margin_limit' not in config:
        return errors, warnings
    margin_limit = float(config.get('global_margin_limit') or 0)
    if margin_limit <= 0:
        if config.get('block_start_without_margin_limit'):
            errors.append(
                'global_margin_limit=0 且 block_start_without_margin_limit=true：'
                '拒绝启动。请设置限额或关闭 block_start_without_margin_limit'
            )
        elif not config.get('allow_margin_limit_disabled'):
            warnings.append(
                'global_margin_limit=0：主循环不会因保证金超限自动暂停新开；'
                '生产环境建议设为正数，或显式 allow_margin_limit_disabled: true'
            )

    return errors, warnings


def load_merged_config(local_path: str = None) -> Dict[str, Any]:
    import ctp_bootstrap

    local_path = local_path or os.path.join(_project_dir(), 'merged_config.yaml')
    pre_cfg = {}
    if os.path.isfile(local_path):
        import yaml
        with open(local_path, 'r', encoding='utf-8') as f:
            pre_cfg = yaml.safe_load(f) or {}
    ctp_bootstrap.setup_paths(pre_cfg)

    from auto_config import load_unified_config, validate_config
    env_cfg = os.environ.get('AUTOTRADE_CONFIG', '').strip() or None
    config = load_unified_config(env_cfg)

    if os.path.isfile(local_path):
        config = _merge_dict(config, pre_cfg)

    for key, default_val in MERGED_TOP_LEVEL_DEFAULTS.items():
        if key not in pre_cfg:
            config[key] = default_val

    dual_cfg = _merge_dict(DUAL_STRATEGY_DEFAULTS, config.get('dual_strategy') or {})
    config['dual_strategy'] = dual_cfg

    strangle_cfg = _merge_dict(STRANGLE_DEFAULTS, config.get('strangle') or {})
    config['strangle'] = strangle_cfg
    config['min_days_to_expiry'] = strangle_cfg.get(
        'min_days_to_expiry', config.get('min_days_to_expiry', 60))
    config['close_days_to_expiry'] = strangle_cfg.get(
        'close_days_to_expiry', config.get('close_days_to_expiry', 30))

    ledger = strangle_cfg.get('ledger_path', 'data/ledger_strangle.json')
    if not os.path.isabs(ledger):
        ledger = os.path.join(_project_dir(), ledger)
    strangle_cfg['ledger_path'] = ledger
    config['strangle']['ledger_path'] = ledger

    ack = config.get('dual_strategy', {}).get('startup_ack_file', 'data/position_startup_ack.txt')
    if not os.path.isabs(ack):
        config.setdefault('dual_strategy', {})['startup_ack_file'] = os.path.join(_project_dir(), ack)

    errors, warnings = validate_config(config)
    merged_errors, merged_warnings = _validate_merged_config(config)
    errors.extend(merged_errors)
    warnings.extend(merged_warnings)
    if errors:
        raise ValueError("配置验证失败:\n" + "\n".join(errors))
    for w in warnings:
        print(f"[CONFIG WARNING] {w}")

    from auto_runtime_profile import attach_runtime_profile
    attach_runtime_profile(config)
    return config


def _install_rotating_log_handler(logger, config: Dict[str, Any]) -> None:
    """把 autotrade setup_logger 装的普通 FileHandler 升级为按天轮转。

    autotrade 的 ``setup_logger`` 在进程启动时按当天命名 ``info/YYYYMMDD.log`` 并
    用普通 FileHandler——7×24 长跑会把多天日志全写进启动日那个文件、永不轮转。
    这里替换为 TimedRotatingFileHandler（每日切分 + backupCount 自动删旧），并把
    日志目录写入 ``config['_log_dir']`` 供 housekeeping 兜底清理遗留旧文件。
    """
    import logging
    from logging.handlers import TimedRotatingFileHandler

    plain = [
        h for h in list(logger.handlers)
        if isinstance(h, logging.FileHandler)
        and not isinstance(h, TimedRotatingFileHandler)
    ]
    if not plain:
        return
    base_file = plain[0].baseFilename
    log_dir = os.path.dirname(base_file)
    formatter = plain[0].formatter
    level = plain[0].level
    retain = int(config.get('log_retain_days', 30) or 30)

    rotating_path = os.path.join(log_dir, 'autoctp.log')
    handler = TimedRotatingFileHandler(
        rotating_path, when='midnight', backupCount=retain, encoding='utf-8',
    )
    handler.setLevel(level)
    if formatter is not None:
        handler.setFormatter(formatter)
    for h in plain:
        logger.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass
    logger.addHandler(handler)
    config['_log_dir'] = log_dir
    logger.info(
        f'[日志] 已启用按天轮转 + 保留 {retain} 天: {rotating_path}'
    )


def setup_merged_logger(config: Dict[str, Any]):
    from pairtrade.config import setup_logger
    logger = setup_logger('AutoCTP', log_level=config.get('log_level', 'INFO'))
    try:
        _install_rotating_log_handler(logger, config)
    except Exception as e:
        logger.warning(f'[日志] 轮转升级失败，沿用原 FileHandler: {e}')
    return logger


def prepare_merged_connection(conn, config: Dict[str, Any]) -> None:
    from auto_strategy_order_ref import init_order_ref_sequences
    init_order_ref_sequences(conn, config)
    config['_spread_fill_conn'] = conn
