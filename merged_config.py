"""配置：autotrade 统一配置 + autoctp 本地 merged_config.yaml。"""

import os
from typing import Any, Dict

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
