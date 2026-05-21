"""配置：autotrade 统一配置 + autoctp 本地 merged_config.yaml。"""

import os
from typing import Any, Dict, Tuple

import ctp_bootstrap

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
    'phase2_timeout': 120,
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
    'spread_reconcile_fallback_heuristic': False,
    'auto_sync_spread_positions_csv': True,

    'unified_fill_feishu': True,
    'fill_feishu_enabled': True,

    'require_startup_ack': True,
    # 生产 7×24：首次人工确认后写盘，冷启动/自动重启不再弹窗阻塞。
    'startup_ack_each_run': False,
    'startup_ack_interactive': True,
    'startup_ack_use_gui': True,
    'startup_ack_prefer_gui': True,
    'startup_ack_force_terminal': False,
    'startup_ack_persist': True,
    # true=确认文件须为当日；false=持久确认跨日有效（改 CSV 后请删文件重确认）
    'startup_ack_require_today': False,
    'startup_ack_file': 'data/position_startup_ack.txt',
    'allow_start_on_reconcile_mismatch': False,
}

# 未在 merged_config.yaml 显式设置时采用的 AutoCTP 顶层默认值
MERGED_TOP_LEVEL_DEFAULTS = {
    'global_margin_limit': 100000,
    'main_loop_max_consecutive_errors': 10,
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


def setup_merged_logger(config: Dict[str, Any]):
    from pairtrade.config import setup_logger
    return setup_logger('AutoCTP', log_level=config.get('log_level', 'INFO'))


def prepare_merged_connection(conn, config: Dict[str, Any]) -> None:
    from auto_strategy_order_ref import init_order_ref_sequences
    init_order_ref_sequences(conn, config)
    config['_spread_fill_conn'] = conn
