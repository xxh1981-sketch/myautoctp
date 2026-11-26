"""Unified spread execution from spread_positions.csv (open, rebalance, close).

Halt paths (by design):
  - Reconcile halt (_spread_open_halted): close-only — ledger untrusted for open/rebalance.
  - Daily limit / margin halt: full process_symbol — ledger trusted; open blocked inside.
"""

from __future__ import annotations

import logging
import sys
from typing import Optional

_log = logging.getLogger(__name__)

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
            from spread_position_sync import converge_flat_spread_claims

            converge_flat_spread_claims(conn, store, symbol, month, config, logger)
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
        runtime = getattr(conn, '_runtime_state', None) or {}
        allow_quarantine_close_only = bool(
            runtime.get('_allow_quarantine_close_only', False),
        )
        if not (
            allow_quarantine_close_only
            and conn._reconnect_quarantine
            and conn.td_logined
            and conn.md_logined
        ):
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
        # 防御：直调 process_symbol 时也拦「空账本 + CTP 残仓」误开
        try:
            from spread_open_preflight import (
                should_block_spread_open_empty_book_ctp_residual,
            )

            if should_block_spread_open_empty_book_ctp_residual(
                conn, item, logger, spread_open_ok=True,
            ):
                sym = str(item.get('future') or '').lower()
                logger.warning(
                    f'[{sym}] 账本无价差认领但 CTP 仍有 Call 残仓，禁止当空仓开仓'
                )
                return False
        except Exception:
            pass
        return _ORIG_PROCESS_SYMBOL(
            conn, item, vix_engine, config, logger, remaining_limit=remaining_limit,
        )

    auto_processor.process_symbol = patched_process_symbol
    # 防御：即使 merged_main_loop 改成 `import auto_processor` 后已经能拿到 patch，
    # 仍然 rebind 一次，覆盖任何遗留的 `from auto_processor import process_symbol` 用法。
    for mod_name in _PROCESS_SYMBOL_CONSUMERS:
        _rebind_module_attr(mod_name, 'process_symbol', patched_process_symbol)


_RISK_CHECK_PATCHED = False
_REBALANCE_CLOSE_A_PATCHED = False


def _dual_isolation_active(config: dict) -> bool:
    """True when spread must isolate strangle-owned legs (ledger or exclusion on)."""
    dual = config.get('dual_strategy') or {}
    return bool(
        spread_execution_from_ledger(config)
        or dual.get('exclude_strangle_from_spread_positions', True)
    )


def _resolve_spread_positions_for_risk(conn, raw_positions, symbol, month, config):
    """Spread-only A/B view for executor risk checks: ledger truth or CTP−strangle.

    Mirrors ``install_spread_analyze_from_ledger._resolve_positions`` so the
    executor never counts strangle long calls on a shared symbol+month as spread A.
    """
    store = store_from_conn(conn)
    if store is not None:
        return build_positions_from_spread_claims(store, conn, symbol, month)
    dual = config.get('dual_strategy') or {}
    if dual.get('exclude_strangle_from_spread_positions', True):
        ledger = _ledger_from_conn(conn)
        vols = merge_strangle_owned_volumes(ledger)
        if vols:
            return exclude_strangle_from_positions(raw_positions, vols, None, symbol)
    return raw_positions


def install_spread_risk_check_exclusion(config: dict) -> None:
    """Wrap executor.check_risk_limits so A/B counts exclude strangle-owned long calls.

    autotrade's ``RiskCheckMixin.check_risk_limits`` reads ``conn.position_tracker``
    (full CTP). On a symbol+month shared with strangle, strangle long calls inflate
    spread ``A_current`` → false "A类超限" / 2:1 failures during stage-3 open and B
    retries. We resolve the tracker's per-symbol view to the spread ledger /
    CTP−strangle only while the original check runs, then restore it.
    """
    global _RISK_CHECK_PATCHED
    if _RISK_CHECK_PATCHED:
        return
    if not _dual_isolation_active(config):
        return

    import auto_executor_select

    cls = auto_executor_select.RiskCheckMixin
    orig_check = cls.check_risk_limits

    def patched_check_risk_limits(
        self, planned_A_groups=1, planned_B_groups=1, enforce_ratio=True,
    ):
        tracker = getattr(self.conn, 'position_tracker', None)
        if tracker is None:
            return orig_check(
                self, planned_A_groups, planned_B_groups, enforce_ratio,
            )
        orig_get = tracker.get_positions_for_symbol
        had_own = 'get_positions_for_symbol' in getattr(tracker, '__dict__', {})

        def filtered_get(symbol, month=None, normalized_month=None):
            raw = orig_get(symbol, month, normalized_month)
            try:
                return _resolve_spread_positions_for_risk(
                    self.conn, raw, symbol, month, self.config,
                )
            except Exception:
                return raw

        tracker.get_positions_for_symbol = filtered_get
        try:
            return orig_check(
                self, planned_A_groups, planned_B_groups, enforce_ratio,
            )
        finally:
            if had_own:
                tracker.get_positions_for_symbol = orig_get
            else:
                tracker.__dict__.pop('get_positions_for_symbol', None)

    cls.check_risk_limits = patched_check_risk_limits
    _RISK_CHECK_PATCHED = True


def install_spread_rebalance_close_a_exclusion(config: dict) -> None:
    """Filter rebalance_close_A_positions candidates through spread ledger / exclusion.

    Under margin-halt, autotrade may sell-close excess A. Its candidate list comes
    from raw CTP long calls (``query_positions_fallback``); on a shared symbol+month
    that could target strangle long calls. We resolve the position source to
    spread-owned legs while the close runs so only spread A legs are sold.
    """
    global _REBALANCE_CLOSE_A_PATCHED
    if _REBALANCE_CLOSE_A_PATCHED:
        return
    if not _dual_isolation_active(config):
        return

    import auto_rebalance

    if not hasattr(auto_rebalance, 'rebalance_close_A_positions'):
        return
    orig_fn = auto_rebalance.rebalance_close_A_positions
    orig_qpf = auto_rebalance.query_positions_fallback

    def patched_rebalance_close_A(
        conn, analysis, symbol, month, min_tick, cfg, logger, vol_of_combo,
    ):
        target_month = month

        def resolver(
            c, timeout=5, logger=None, symbol=None,
            month=None, normalized_month=None,
        ):
            raw = orig_qpf(
                c, timeout=timeout, logger=logger, symbol=symbol,
                month=month, normalized_month=normalized_month,
            )
            if not raw:
                return raw
            eff_month = month if month is not None else target_month
            try:
                return _resolve_spread_positions_for_risk(
                    c, raw, symbol, eff_month, cfg or config,
                )
            except Exception:
                return raw

        auto_rebalance.query_positions_fallback = resolver
        try:
            return orig_fn(
                conn, analysis, symbol, month, min_tick, cfg, logger, vol_of_combo,
            )
        finally:
            auto_rebalance.query_positions_fallback = orig_qpf

    auto_rebalance.rebalance_close_A_positions = patched_rebalance_close_A
    _REBALANCE_CLOSE_A_PATCHED = True


def _surface_leg_pairing_install_failure(config: dict) -> None:
    from strangle_leg_pairing import get_install_error

    reason = get_install_error() or '未知原因'
    msg = (
        f'宽跨 leg_claims 配对补丁未安装：{reason}。'
        'surplus leg_claims 不会推断配对/单腿平仓或补腿，裸腿风险防护隐性失效；'
        '建议检查 autostraggle 版本与 sys.path 后再启动。'
    )
    _log.error('[启动自检] %s', msg)
    try:
        from auto_feishu import send_feishu_message

        send_feishu_message(
            f'⚠️ **AutoCTP 启动自检告警**\n\n{msg}',
            config=config,
        )
    except Exception as notify_err:
        _log.warning(
            '宽跨配对补丁未安装飞书通知失败: %s', notify_err, exc_info=True,
        )
    if config.get('fail_fast_on_guard_install', False):
        _log.error('[启动自检] fail_fast_on_guard_install=true，拒绝启动')
        sys.exit(4)


def _surface_session_close_guard_install_failure(config: dict, reason: str) -> None:
    """收盘守卫是 T-10 禁新组 / T-1 硬停撤单的盘中硬边界，安装失败须与
    原子保存 / 月白名单 / CTP 三补丁同级：error + 飞书 + honor fail_fast。"""
    msg = (
        f'小节收盘守卫未安装：{reason or "未知原因"}。'
        'T-10 禁新组开/平与 T-1 硬停撤单将失效（收盘前可能发出无法成交/'
        '无法撤销的挂单）；建议检查 autotrade 版本与 sys.path 后再启动。'
    )
    _log.error('[启动自检] %s', msg)
    try:
        from auto_feishu import send_feishu_message

        send_feishu_message(
            f'⚠️ **AutoCTP 启动自检告警**\n\n{msg}',
            config=config,
        )
    except Exception as notify_err:
        _log.warning(
            '收盘守卫未安装飞书通知失败: %s', notify_err, exc_info=True,
        )
    if config.get('fail_fast_on_guard_install', False):
        _log.error('[启动自检] fail_fast_on_guard_install=true，拒绝启动')
        sys.exit(4)


def install_spread_ledger_execution(config: dict) -> None:
    """Install all spread ledger-driven execution patches (idempotent)."""
    global _INSTALLED
    if _INSTALLED:
        return
    install_spread_analyze_from_ledger(config)
    install_spread_close_from_ledger(config)
    install_spread_rebalance_from_ledger(config)
    install_spread_process_symbol_halt(config)
    install_spread_risk_check_exclusion(config)
    install_spread_rebalance_close_a_exclusion(config)
    try:
        from session_close_guard import (
            get_install_error as _scg_install_error,
            install_session_close_guard,
        )
        if not install_session_close_guard(config):
            _surface_session_close_guard_install_failure(
                config, _scg_install_error() or '未知原因',
            )
    except SystemExit:
        raise
    except Exception as e:
        _surface_session_close_guard_install_failure(config, repr(e))
    from strangle_leg_pairing import install_strangle_leg_pairing_patch

    if not install_strangle_leg_pairing_patch():
        _surface_leg_pairing_install_failure(config)
        return
    _INSTALLED = True


def set_spread_open_halt(conn, halted: bool, reason: str = '') -> None:
    runtime = getattr(conn, '_runtime_state', None)
    if runtime is None:
        return
    runtime['_spread_open_halted'] = bool(halted)
    runtime['_spread_open_halt_reason'] = reason or ''
