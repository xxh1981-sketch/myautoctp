"""Minimal autotrade module stubs for CI unit tests (no D:\\autotrade required)."""

from __future__ import annotations

import os
import re
import sys
import types
from typing import Iterable


def _extract_symbol_prefix(inst: str) -> str:
    m = re.match(r'^([A-Za-z]+)', str(inst or ''))
    return m.group(1).lower() if m else ''


def _extract_month_from_contract(inst: str) -> str | None:
    s = str(inst or '').upper()
    m = re.search(r'^[A-Z]+(\d{3,4})', s.replace('-', ''))
    if m:
        return m.group(1)
    m = re.search(r'^[A-Z]+\d{3,4}', s)
    if m:
        tail = m.group(0)
        digits = re.search(r'(\d{3,4})', tail)
        return digits.group(1) if digits else None
    return None


def _months_match(inst: str, month: str, normalized_month: str) -> bool:
    s = str(inst or '')
    return str(month) in s or str(normalized_month) in s


def _extract_strike_from_instrument(inst: str, normalized_month: str, option_type: str = 'C'):
    u = str(inst or '').upper()
    if option_type == 'C':
        if re.search(r'C\d', u) and not re.search(r'P\d', u.split('C')[0]):
            return 2400
        if re.search(r'C\d', u) and 'P' not in u:
            return 2400
        return None
    return None


def _sum_positions_margin_for_limit(conn, pos, config):
    return (0, {})


def _contract_case_variants(inst: str):
    s = str(inst or '').strip()
    if not s:
        return []
    return list(dict.fromkeys([s, s.upper(), s.lower()]))


def _install_strategy_order_ref(mod):
    mod.DEFAULT_STRANGLE_ORDER_REF_MIN = 500000
    mod.STRATEGY_SPREAD = 'spread'
    mod.STRATEGY_STRANGLE = 'strangle'

    def get_strangle_order_ref_min(config):
        return int(config.get('strangle', {}).get('order_ref_min', 500000))

    def get_spread_order_ref_max(config):
        dual = config.get('dual_strategy') or {}
        return int(dual.get('spread_order_ref_max', 499999))

    def is_strangle_order_ref(order_ref, config):
        try:
            return int(order_ref) >= get_strangle_order_ref_min(config)
        except (TypeError, ValueError):
            return False

    def is_spread_order_ref(order_ref, config):
        try:
            ref = int(order_ref)
        except (TypeError, ValueError):
            return False
        return 0 < ref < get_strangle_order_ref_min(config)

    def init_order_ref_sequences(conn, config):
        conn._spread_order_ref_seq = 0
        conn._strangle_order_ref_seq = get_strangle_order_ref_min(config) - 1

    def allocate_order_ref(conn, strategy, config):
        if strategy == mod.STRATEGY_SPREAD:
            conn._spread_order_ref_seq = int(getattr(conn, '_spread_order_ref_seq', 0)) + 1
            return conn._spread_order_ref_seq
        conn._strangle_order_ref_seq = int(getattr(conn, '_strangle_order_ref_seq', 0)) + 1
        return conn._strangle_order_ref_seq

    mod.get_strangle_order_ref_min = get_strangle_order_ref_min
    mod.get_spread_order_ref_max = get_spread_order_ref_max
    mod.is_strangle_order_ref = is_strangle_order_ref
    mod.is_spread_order_ref = is_spread_order_ref
    mod.init_order_ref_sequences = init_order_ref_sequences
    mod.allocate_order_ref = allocate_order_ref


def _install_auto_order_manager(mod):
    class OrderManager:
        def send_order(
            self,
            instrument,
            direction,
            volume,
            price,
            offset='0',
            hedge='1',
            assert_no_pending=False,
            strategy='spread',
        ):
            return None, None

    mod.OrderManager = OrderManager


def _install_auto_processor(mod):
    mod.process_symbol = lambda *a, **kw: False
    mod.is_trading_time = lambda symbol: True


def _install_auto_initializer(mod):
    mod.manage_future_price_readiness = lambda *a, **kw: None


def _install_auto_feishu(mod):
    def send_feishu_message(message, config=None):
        return True

    def notify_order_filled(*args, **kwargs):
        return True

    class FeishuNotifier:
        @staticmethod
        def notify_order_filled(*args, **kwargs):
            return True

    def get_notifier(config=None):
        return FeishuNotifier()

    def safe_notify(name, *args, **kwargs):
        return None

    mod.send_feishu_message = send_feishu_message
    mod.notify_order_filled = notify_order_filled
    mod.FeishuNotifier = FeishuNotifier
    mod.get_notifier = get_notifier
    mod.safe_notify = safe_notify


def _install_auto_feishu_command(mod):
    mod.start_command_receiver = lambda *a, **kw: None
    mod.stop_command_receiver = lambda *a, **kw: None
    mod.is_trading_paused = lambda: False


def _install_auto_scheduled_pause(mod):
    def log_main_loop_offline_skip(conn, config, logger, quarantine=False):
        if logger is None:
            return
        if quarantine:
            logger.info('[主循环] 重连隔离期，跳过本轮交易扫描')
        elif not getattr(conn, 'td_logined', False) or not getattr(conn, 'md_logined', False):
            logger.info('[主循环] CTP 未全部登录，跳过本轮')

    mod.sync_connection_suspend_state = lambda *a, **kw: None
    mod.is_connection_suspended = lambda *a, **kw: False
    mod.log_main_loop_offline_skip = log_main_loop_offline_skip


def _install_auto_scheduled_reconnect(mod):
    mod.check_scheduled_full_recovery = lambda *a, **kw: False


def _install_auto_circuit_breaker(mod):
    class CircuitBreaker:
        def __init__(self, *a, **kw):
            pass

    mod.CircuitBreaker = CircuitBreaker


def _install_auto_health_check(mod):
    class HealthChecker:
        def __init__(self, conn, config, logger):
            self.conn = conn

        def check_now(self, force=False):
            return {'healthy': True}

        def get_summary(self):
            return 'ok'

    mod.HealthChecker = HealthChecker


def _install_auto_reconnect_recovery(mod):
    mod.check_quarantine_watchdog = lambda *a, **kw: None


def _install_straggle_execution(mod):
    class StrangleExecutor:
        def __init__(self, *a, **kw):
            pass

        def run_rebalance(self, *a, **kw):
            return None

    mod.StrangleExecutor = StrangleExecutor


def _install_straggle_processor(mod):
    mod.process_strangle_symbol = lambda *a, **kw: False


def _install_straggle_reconcile(mod):
    mod.reconcile_strangle_positions = lambda *a, **kw: (False, [])


_STUB_BUILDERS = {
    'auto_connection': lambda mod: (
        setattr(mod, 'extract_symbol_prefix', _extract_symbol_prefix),
        setattr(mod, 'months_match', _months_match),
    ),
    'auto_connection_utils': lambda mod: (
        setattr(mod, 'months_match', _months_match),
        setattr(mod, 'contract_case_variants', _contract_case_variants),
        setattr(mod, 'extract_month_from_contract', _extract_month_from_contract),
    ),
    'auto_order_manager': _install_auto_order_manager,
    'auto_position': lambda mod: setattr(
        mod, 'extract_strike_from_instrument', _extract_strike_from_instrument,
    ),
    'auto_risk': lambda mod: setattr(
        mod, 'sum_positions_margin_for_limit', _sum_positions_margin_for_limit,
    ),
    'auto_strategy_order_ref': _install_strategy_order_ref,
    'auto_processor': _install_auto_processor,
    'auto_initializer': _install_auto_initializer,
    'auto_feishu': _install_auto_feishu,
    'auto_feishu_command': _install_auto_feishu_command,
    'auto_scheduled_pause': _install_auto_scheduled_pause,
    'auto_scheduled_reconnect': _install_auto_scheduled_reconnect,
    'auto_circuit_breaker': _install_auto_circuit_breaker,
    'auto_health_check': _install_auto_health_check,
    'auto_reconnect_recovery': _install_auto_reconnect_recovery,
}

_AUTOSTRAGGLE_STUB_BUILDERS = {
    'straggle_execution': _install_straggle_execution,
    'straggle_processor': _install_straggle_processor,
    'straggle_reconcile': _install_straggle_reconcile,
}


ALL_STUB_MODULES = tuple(_STUB_BUILDERS.keys())
AUTOSTRAGGLE_STUB_MODULES = tuple(_AUTOSTRAGGLE_STUB_BUILDERS.keys())


def _should_use_stubs(env_root_key: str, default_path: str) -> bool:
    if os.environ.get('AUTOCTP_ALLOW_MISSING_DEPS', '').strip() == '1':
        root = os.environ.get(env_root_key, '').strip()
        return not (root and os.path.isdir(root))
    root = os.environ.get(env_root_key, '').strip()
    if root and os.path.isdir(root):
        return False
    return not os.path.isdir(default_path)


def _register_stub_modules(builders: dict, modules: Iterable[str]) -> None:
    for name in modules:
        existing = sys.modules.get(name)
        if existing is not None and getattr(existing, '__file__', None):
            continue
        mod = existing if existing is not None else types.ModuleType(name)
        builder = builders.get(name)
        if builder:
            builder(mod)
        sys.modules[name] = mod


def ensure_auto_feishu_stub() -> None:
    """Ensure ``auto_feishu`` exists for ``@patch('auto_feishu....')`` in unit tests.

    Prefer the real autotrade module when ``AUTOTRADE_ROOT`` is on ``sys.path``;
    otherwise install a minimal in-memory stub (CI pytest-unit / Windows).
    """
    name = 'auto_feishu'
    existing = sys.modules.get(name)
    if existing is not None and getattr(existing, '__file__', None):
        return
    try:
        import importlib
        importlib.import_module(name)
        mod = sys.modules.get(name)
        if mod is not None and getattr(mod, '__file__', None):
            return
    except ImportError:
        pass
    mod = existing if existing is not None else types.ModuleType(name)
    _install_auto_feishu(mod)
    sys.modules[name] = mod


def ensure_autotrade_stubs(modules: Iterable[str] | None = None) -> None:
    """Register stub modules only when not already imported (real autotrade wins)."""
    if not _should_use_stubs('AUTOTRADE_ROOT', r'D:\autotrade'):
        return
    ensure_auto_feishu_stub()
    _register_stub_modules(_STUB_BUILDERS, modules or ALL_STUB_MODULES)


def ensure_autostraggle_stubs(modules: Iterable[str] | None = None) -> None:
    """Register minimal autostraggle stubs for merged_main_loop unit tests."""
    if not _should_use_stubs('AUTOSTRAGGLE_ROOT', r'D:\autostraggle'):
        return
    _register_stub_modules(_AUTOSTRAGGLE_STUB_BUILDERS, modules or AUTOSTRAGGLE_STUB_MODULES)


def ensure_merged_loop_stubs() -> None:
    """Install autotrade + autostraggle stubs needed by merged_main_loop tests."""
    ensure_autotrade_stubs(ALL_STUB_MODULES)
    ensure_autostraggle_stubs()
