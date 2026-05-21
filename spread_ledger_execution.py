"""Unified spread execution from spread_positions.csv (open, rebalance, close).

Halt paths (by design):
  - Reconcile halt (_spread_open_halted): close-only — ledger untrusted for open/rebalance.
  - Daily limit / margin halt: full process_symbol — ledger trusted; open blocked inside.
"""

from __future__ import annotations

from typing import Optional

from spread_close_ledger import (
    build_positions_from_spread_claims,
    install_spread_close_from_ledger,
    store_from_conn,
)
from spread_dual_config import spread_execution_from_ledger
from spread_position_adjust import (
    _ledger_from_conn,
    _rebind_auto_processor_attr,
    exclude_strangle_from_positions,
    install_spread_excludes_strangle,
    merge_strangle_owned_volumes,
)

_ORIG_PROCESS_SYMBOL = None
_ORIG_REBALANCE_ONE_LEG = None
_INSTALLED = False

# 任何 `from auto_processor import process_symbol` / `from auto_closer import process_close` 等
# 已绑定的本地名字都不会被 `auto_processor.process_symbol = patched` 自动更新。把所有已知
# 直接 from-import 关键函数的下游模块列在此处，install_* 时显式 rebind。
_PROCESS_SYMBOL_CONSUMERS = ('merged_main_loop',)


def _rebind_module_attr(module_name: str, attr: str, value) -> int:
    """Reassign ``module_name.attr = value`` if the module has been imported.

    Returns 1 when the rebind happened, 0 otherwise. Used to compensate for
    Python's `from X import Y` semantics, which would otherwise leave a stale
    function reference on downstream modules even after we patch ``X.Y``.
    """
    import sys

    mod = sys.modules.get(module_name)
    if mod is None or not hasattr(mod, attr):
        return 0
    setattr(mod, attr, value)
    return 1


def _rebind_analyze_consumers(patched_analyze, patched_check=None) -> None:
    _rebind_auto_processor_attr('analyze_position_imbalance', patched_analyze)
    import sys

    reb = sys.modules.get('auto_rebalance')
    if reb is not None:
        reb.analyze_position_imbalance = patched_analyze
        if patched_check is not None and hasattr(reb, 'check_position_limits'):
            reb.check_position_limits = patched_check


def install_spread_analyze_from_ledger(config: dict) -> None:
    """Patch A/B analysis to read SpreadLegStore instead of CTP (open + rebalance)."""
    if not spread_execution_from_ledger(config):
        install_spread_excludes_strangle(config)
        return

    import spread_position_adjust as spa

    if spa._ORIG_ANALYZE is not None:
        return

    import auto_position

    spa._ORIG_ANALYZE = auto_position.analyze_position_imbalance
    spa._ORIG_CHECK_LIMITS = auto_position.check_position_limits
    orig_analyze = spa._ORIG_ANALYZE
    orig_check = spa._ORIG_CHECK_LIMITS

    def _resolve_positions(conn, positions, symbol, month, config, logger):
        store = store_from_conn(conn)
        if store is not None:
            ledger_pos = build_positions_from_spread_claims(store, conn, symbol, month)
            if logger:
                if ledger_pos:
                    logger.debug(
                        f'[{symbol}] spread A/B from ledger ({len(ledger_pos)} legs)'
                    )
                else:
                    logger.debug(f'[{symbol}] spread A/B from ledger (empty)')
            return ledger_pos

        dual = config.get('dual_strategy') or {}
        if dual.get('exclude_strangle_from_spread_positions', True):
            ledger = _ledger_from_conn(conn)
            vols = merge_strangle_owned_volumes(ledger)
            if vols:
                return exclude_strangle_from_positions(
                    positions, vols, logger, symbol,
                )
        return positions

    def patched_analyze(
        conn, positions, symbol, month, vol_of_combo, config, future_price, logger,
    ):
        positions = _resolve_positions(conn, positions, symbol, month, config, logger)
        return orig_analyze(
            conn, positions, symbol, month, vol_of_combo, config, future_price, logger,
        )

    def patched_check(conn, positions, symbol, month, vol_of_combo, config):
        positions = _resolve_positions(conn, positions, symbol, month, config, None)
        return orig_check(conn, positions, symbol, month, vol_of_combo, config)

    auto_position.analyze_position_imbalance = patched_analyze
    auto_position.check_position_limits = patched_check
    _rebind_analyze_consumers(patched_analyze, patched_check)


def _spread_daily_open_count(conn, config: dict, timeout: float = 2) -> Optional[int]:
    from spread_fill_sync import count_spread_filled_open_orders

    count = count_spread_filled_open_orders(conn, config, timeout=timeout)
    if count is not None:
        return count
    return conn.get_filled_open_order_count(timeout=timeout)


def install_spread_rebalance_from_ledger(config: dict) -> None:
    """Rebalance retry loop uses spread OrderRef daily count + ledger A/B via analyze patch."""
    global _ORIG_REBALANCE_ONE_LEG
    if not spread_execution_from_ledger(config):
        return
    if _ORIG_REBALANCE_ONE_LEG is not None:
        return

    import auto_rebalance

    _ORIG_REBALANCE_ONE_LEG = auto_rebalance._rebalance_one_leg

    def patched_rebalance_one_leg(conn, *args, **kwargs):
        config_obj = kwargs.get('config')
        if config_obj is None and len(args) >= 6:
            config_obj = args[5]
        orig_get = conn.get_filled_open_order_count

        def spread_get(timeout=2):
            if config_obj is not None:
                count = _spread_daily_open_count(conn, config_obj, timeout=timeout)
                if count is not None:
                    return count
            return orig_get(timeout=timeout)

        conn.get_filled_open_order_count = spread_get
        try:
            return _ORIG_REBALANCE_ONE_LEG(conn, *args, **kwargs)
        finally:
            conn.get_filled_open_order_count = orig_get

    auto_rebalance._rebalance_one_leg = patched_rebalance_one_leg


def _spread_close_only(conn, item, vix_engine, config, logger) -> bool:
    """Run spread close path only (when spread reconcile halted open/rebalance)."""
    from auto_closer import process_close
    from auto_processor import is_trading_time

    symbol = item['future']
    profile = config.get('_runtime_profile') or {}
    enforce_hours = profile.get(
        'enforce_trading_hours',
        not config.get('is_simulation'),
    )
    if enforce_hours and not is_trading_time(symbol):
        return False
    if conn._reconnect_quarantine or not conn.td_logined or not conn.md_logined:
        return False

    sym = symbol.lower()
    vix = vix_engine.calculate_vix(sym, conn, logger)
    effective_vix = vix if vix is not None else float('inf')
    try:
        positions = conn.query_positions_sync()
    except Exception:
        positions = None
    return process_close(
        conn, item, effective_vix, config, logger, positions=positions,
    )


def install_spread_process_symbol_halt(config: dict) -> None:
    """When spread reconcile halts, still allow close but skip open/rebalance."""
    global _ORIG_PROCESS_SYMBOL
    if not spread_execution_from_ledger(config):
        return
    if _ORIG_PROCESS_SYMBOL is not None:
        return

    import auto_processor

    _ORIG_PROCESS_SYMBOL = auto_processor.process_symbol

    def patched_process_symbol(
        conn, item, vix_engine, config, logger, remaining_limit=None,
    ):
        runtime = getattr(conn, '_runtime_state', None) or {}
        if runtime.get('_spread_open_halted'):
            return _spread_close_only(conn, item, vix_engine, config, logger)
        return _ORIG_PROCESS_SYMBOL(
            conn, item, vix_engine, config, logger, remaining_limit=remaining_limit,
        )

    auto_processor.process_symbol = patched_process_symbol
    # 防御：即使 merged_main_loop 改成 `import auto_processor` 后已经能拿到 patch，
    # 仍然 rebind 一次，覆盖任何遗留的 `from auto_processor import process_symbol` 用法。
    for mod_name in _PROCESS_SYMBOL_CONSUMERS:
        _rebind_module_attr(mod_name, 'process_symbol', patched_process_symbol)


def install_spread_ledger_execution(config: dict) -> None:
    """Install all spread ledger-driven execution patches (idempotent)."""
    global _INSTALLED
    if _INSTALLED:
        return
    install_spread_analyze_from_ledger(config)
    install_spread_close_from_ledger(config)
    install_spread_rebalance_from_ledger(config)
    install_spread_process_symbol_halt(config)
    _INSTALLED = True


def set_spread_open_halt(conn, halted: bool, reason: str = '') -> None:
    runtime = getattr(conn, '_runtime_state', None)
    if runtime is None:
        return
    runtime['_spread_open_halted'] = bool(halted)
    runtime['_spread_open_halt_reason'] = reason or ''
