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
    'unmatched_leg_metadata_alert': True,
    'unmatched_leg_metadata_alert_cooldown_sec': 1800,
}


def _project_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def merged_config_local_path() -> str:
    """Absolute path to ``merged_config.yaml`` in the project root."""
    return os.path.join(_project_dir(), 'merged_config.yaml')


def _merge_dict(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = base.copy()
    for key, value in override.items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _merge_dict(out[key], value)
        else:
            out[key] = value
    return out


def _as_non_negative_float(value: Any) -> Tuple[bool, float]:
    try:
        out = float(value)
    except Exception:
        return False, 0.0
    return out >= 0, out


def _as_non_negative_int(value: Any) -> Tuple[bool, int]:
    try:
        out = int(value)
    except Exception:
        return False, 0
    return out >= 0, out


DUAL_STRATEGY_DEFAULTS = {
    'strategy_order': ['spread', 'strangle'],
    'spread_order_ref_max': 499999,

    'journal_daily_shards': True,
    'journal_retain_days': 14,
    'trade_replay_lookback_days': 1,
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
    'strangle_fill_require_tradeinfo_match': True,
    'strangle_fill_skip_spread_owned_instruments': True,
    'spread_derive_require_tradeinfo_match': True,
    'spread_purge_invalid_claims_on_startup': True,
    'spread_reconcile_fallback_heuristic': False,
    'auto_sync_spread_positions_csv': True,
    'reconcile_diagnostic_snapshot_enabled': True,
    'reconcile_diagnostic_issue_limit': 8,
    'ctp_unknown_direction_warn': True,
    'strangle_store_unavailable_fallback': 'skip_fill',
    'orderref_bilateral_nonzero_alert': True,
    'orderref_bilateral_nonzero_alert_cooldown_sec': 1800,

    'unified_fill_feishu': True,
    'fill_feishu_enabled': True,
    # unified 成交开启时压制 notify_combo_done / notify_position_closed 等旧摘要
    'suppress_legacy_fill_feishu': True,
    # 操作性重复告警（拒单/流动性/平仓残留等）飞书冷却秒数；0=不冷却
    'feishu_alert_cooldown_sec': 300,

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
    # 磁盘空间预检：housekeeping 周期检查剩余空间，低于阈值飞书告警。
    'disk_space_check_enabled': True,
    'disk_space_warn_mb': 500,
    # 单品种扫描超时（秒）；0=不限制。防止单品种 CTP/IO 挂死阻塞整轮。
    'max_symbol_scan_sec': 120,
    # 周末非交易抑制（仅双休日；法定节假日不处理，当交易日）。周六仅在此时刻
    # 之后才算周末，避开周五夜盘跨零点到周六凌晨。
    'weekend_pause_enabled': True,
    'weekend_pause_saturday_from_hour': 5,
    # 单轮看门狗：一轮耗时超阈值告警（与 merged_config.yaml 默认 60 对齐）。
    'round_slow_warn_sec': 60,
    # 宽跨每轮扫描时间预算（秒）；0=不限制。仅 defer 后续品种，不中断当前品种。
    # 单品种硬超时见 max_symbol_scan_sec（默认 120s）。
    'max_strangle_scan_sec': 600,
    'health_offline_log_cooldown_sec': 60,
    'slow_round_alert_cooldown_sec': 300,
    # 无人值守心跳：主循环每轮覆写心跳文件（供外部 watchdog 检查 mtime，发现
    # 整进程挂死/被 kill——进程内重启循环覆盖不了这两类）；每天首次到达
    # daily_heartbeat_hour 后发一条飞书"报平安"状态摘要，用"没收到"反向发现
    # 程序或飞书链路整体故障。heartbeat_file 置空字符串可禁用心跳文件。
    'heartbeat_file': 'data/main_loop_heartbeat.txt',
    'daily_heartbeat_enabled': True,
    'daily_heartbeat_hour': 9,
    'daily_heartbeat_marker_file': 'data/daily_heartbeat_sent.txt',
    # SIGTERM 优雅关闭最大等待秒数；超时 os._exit（含短连+双轮撤单，默认 45s）。
    'shutdown_timeout_sec': 45,
    # 退出前等待品种扫描后台线程（秒）；无法强杀时靠发单守卫 + 双轮撤单兜底。
    'shutdown_scan_drain_sec': 5,
    'shutdown_cancel_passes': 2,
    'shutdown_cancel_pass_pause_sec': 1.0,
    'shutdown_cancel_confirm_timeout': 15,
    'shutdown_cancel_login_timeout': 30,
    # 最近一次启动/重启/退出原因（纯可观测性）。
    'restart_reason_file': 'data/restart_reason.txt',
    # 外部 watchdog（scripts/check_heartbeat_watchdog.*）读取的配置。
    'heartbeat_watchdog': {
        'alert_after_sec': 300,
        'alert_cooldown_sec': 600,
        'feishu_webhook': '',
    },
    # 长跑时 merged_config.yaml 被编辑但未重启 → 周期性飞书提醒（纯可观测性）。
    'config_drift_alert_enabled': True,
    'config_drift_check_interval_sec': 600,
    # 各类 halt 从 True→False 时飞书通知解除（纯可观测性，不改 halt 语义）。
    'halt_recovery_notify_enabled': True,
    'halt_recovery_alert_cooldown_sec': 300,
    # 运维维护模式：继续监控/对账/告警，禁止一切自动发单与撤单（见 maintenance_mode.py）
    'maintenance_mode': False,
    'maintenance_mode_file': 'data/maintenance_mode.flag',
    # 持仓 CSV 完整性：空表合法；损坏 → position_csv_halt（禁新开）
    'position_csv_integrity_enabled': True,
    'position_csv_check_interval_sec': 3600,
    'fail_fast_on_position_csv_corrupt': False,
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
    'session_close_guard': {
        'enabled': True,
        'no_new_group_before_close_sec': 600,
        'hard_stop_before_close_sec': 60,
        'include_morning_break': False,
    },
    # 日志降噪：节流 autotrade VIX 重复提示（同品种一轮十余次）+ 把非交易态
    # "撤单 当前状态禁止此项操作" 这类预期回报从 ERROR 降为 WARNING。
    # 仅作用于日志输出，不改 VIX 算法 / 每轮缓存 / autotrade 代码；ERROR 及以上
    # 永不被节流，交易动作类日志不在匹配范围。
    'log_noise': {
        'enabled': True,
        'throttle_window_sec': 60,
        'throttle_key_mode': 'substring',
        'throttle_substrings': [
            '提升次近月为近月',
            '品种整体 VIX 无法计算',
            'VIX无法计算',
            '无法找到合适的期权组合',
            '风控阻止开仓',
            '单轮耗时',
            '[健康] 交易连接断开',
            '[健康] 行情连接断开',
            '持仓追踪器超过',
            'analyze_position_imbalance 计算结果',
            '不平衡检查:',
        ],
        'downgrade': [
            {'substring': '当前状态禁止此项操作', 'to_level': 'WARNING'},
            {'substring': '配对失败', 'to_level': 'WARNING'},
            {'substring': '不允许重复报单', 'to_level': 'WARNING'},
            {'substring': '订单不在pending且查询无结果', 'to_level': 'WARNING'},
        ],
    },
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

    if dual.get('pause_spread_open_on_reconcile_mismatch') is False:
        warnings.append(
            'dual_strategy.pause_spread_open_on_reconcile_mismatch=false：'
            '价差对账不一致时不 halt（仍写入 _spread_reconcile_halt），'
            '开仓/再平衡照常；仅审计告警模式，实盘慎用'
        )
    str_cfg = config.get('strangle') or {}
    if str_cfg.get('pause_open_on_reconcile_mismatch') is False:
        warnings.append(
            'strangle.pause_open_on_reconcile_mismatch=false：'
            '宽跨对账不一致时不 set ledger.open_halted，新开照常；'
            '仅审计告警模式，实盘慎用'
        )

    for key in (
        'reconcile_diagnostic_snapshot_enabled',
        'ctp_unknown_direction_warn',
        'orderref_bilateral_nonzero_alert',
    ):
        if key in dual and not isinstance(dual.get(key), bool):
            errors.append(f'dual_strategy.{key} 必须为布尔值')

    if 'reconcile_diagnostic_issue_limit' in dual:
        ok, _value = _as_non_negative_int(dual.get('reconcile_diagnostic_issue_limit'))
        if not ok:
            errors.append('dual_strategy.reconcile_diagnostic_issue_limit 必须为整数')

    if 'orderref_bilateral_nonzero_alert_cooldown_sec' in dual:
        ok, _value = _as_non_negative_float(
            dual.get('orderref_bilateral_nonzero_alert_cooldown_sec'),
        )
        if not ok:
            errors.append('dual_strategy.orderref_bilateral_nonzero_alert_cooldown_sec 必须为数字')

    fallback_values = {'allow', 'skip_fill'}
    if 'strangle_store_unavailable_fallback' in dual:
        fallback = dual.get('strangle_store_unavailable_fallback')
        if not isinstance(fallback, str) or fallback not in fallback_values:
            errors.append(
                'dual_strategy.strangle_store_unavailable_fallback 必须为 '
                f'{sorted(fallback_values)}'
            )
        elif fallback == 'allow':
            warnings.append(
                'dual_strategy.strangle_store_unavailable_fallback=allow：'
                'SpreadLegStore 不可用时宽跨成交会继续入账（fail-open）；'
                '无人值守默认 skip_fill'
            )

    if 'unmatched_leg_metadata_alert' in str_cfg and not isinstance(
        str_cfg.get('unmatched_leg_metadata_alert'), bool,
    ):
        errors.append('strangle.unmatched_leg_metadata_alert 必须为布尔值')

    if 'unmatched_leg_metadata_alert_cooldown_sec' in str_cfg:
        ok, _value = _as_non_negative_float(
            str_cfg.get('unmatched_leg_metadata_alert_cooldown_sec'),
        )
        if not ok:
            errors.append('strangle.unmatched_leg_metadata_alert_cooldown_sec 必须为数字')

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
    # 降噪过滤器：节流 autotrade VIX 重复提示 + 把非交易态撤单回报降级为 WARNING。
    # 失败仅告警（退回原始噪音输出），不致命。
    try:
        from log_noise_filter import install_log_noise_filter, get_install_error
        if not install_log_noise_filter(logger, config):
            logger.warning(
                '[日志] 降噪过滤器未安装: %s（沿用原始日志输出）',
                get_install_error() or '未知原因',
            )
    except Exception as e:
        logger.warning(f'[日志] 降噪过滤器安装异常: {e}')
    return logger


def prepare_merged_connection(conn, config: Dict[str, Any]) -> None:
    from auto_strategy_order_ref import init_order_ref_sequences
    init_order_ref_sequences(conn, config)
    config['_spread_fill_conn'] = conn
    try:
        from maintenance_mode import (
            install_maintenance_guard,
            wrap_connection_cancel_guard,
        )
        install_maintenance_guard(config)
        wrap_connection_cancel_guard(conn, config)
    except Exception:
        pass
