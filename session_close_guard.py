"""T-10 / T-1 session close guard (AutoCTP).

T-10 (default 600s before segment end):
  - Block **new group** open / close (spread stage-3 open, spread new process_close,
    strangle new execute_open / execute_close on ``open`` positions).
  - Allow: spread A/B rebalance, in-progress spread combo (``_active_executor`` /
    ``_closing_lock``), strangle ``awaiting_phase2`` / ``close_chp_pending`` via
    ``run_rebalance``.

T-1 (default 60s before segment end):
  - Block all new sends (``send_order`` + ``auto_closer_executor._send_and_wait`` backstop).
  - Skip spread / strangle symbol scans and rebalance.
  - Cancel pending orders for affected symbols (only while in session).
  - Set per-symbol T-1 abort flag so in-progress spread close retries stop sending.
  - Prune local ``pending_orders`` ghosts via exchange order query.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import List, Optional, Set, Tuple

from session_close_calendar import (
    get_session_phase,
    is_trading_time_at,
    segment_end_id,
    seconds_to_segment_end,
)

_log = logging.getLogger(__name__)

_INSTALLED = False
_INSTALL_ERROR: Optional[str] = None

_PENDING_STATUS = frozenset({'1', '3', 'a', 'b', 'c'})
_T1_CANCEL_PREFIX = '_session_t1_cancel_at'
_T1_ABORT_PREFIX = '_session_t1_abort_at'
_T10_LOG_PREFIX = '_session_t10_log_at'
_SPREAD_CLOSE_ACTIVE_PREFIX = '_spread_close_active'
_LOG_COOLDOWN = 300.0

OPEN_INCOMPLETE_KINDS = frozenset({'awaiting_phase2'})
CLOSE_INCOMPLETE_KINDS = frozenset({'close_chp_pending'})


def is_installed() -> bool:
    return _INSTALLED


def get_install_error() -> Optional[str]:
    return _INSTALL_ERROR


def guard_enabled(config: Optional[dict]) -> bool:
    return bool(((config or {}).get('session_close_guard') or {}).get('enabled', True))


def _maybe_log_throttled(conn, key: str, logger, level: str, msg: str) -> None:
    runtime = getattr(conn, '_runtime_state', None) or {}
    now = time.time()
    full = f'{_T10_LOG_PREFIX}:{key}'
    if now - float(runtime.get(full, 0) or 0) < _LOG_COOLDOWN:
        return
    runtime[full] = now
    if logger is None:
        return
    getattr(logger, level, logger.info)(msg)


def strangle_has_incomplete_open(ledger, symbol: str) -> bool:
    if ledger is None:
        return False
    sym = symbol.lower()
    for item in ledger.list_unmatched_legs() or []:
        if (item.get('symbol') or '').lower() != sym:
            continue
        if item.get('kind') in OPEN_INCOMPLETE_KINDS:
            return True
    return False


def strangle_has_incomplete_close(ledger, symbol: str) -> bool:
    if ledger is None:
        return False
    sym = symbol.lower()
    for item in ledger.list_unmatched_legs() or []:
        if (item.get('symbol') or '').lower() != sym:
            continue
        if item.get('kind') in CLOSE_INCOMPLETE_KINDS:
            return True
    return False


def spread_combo_in_progress(conn, symbol: str) -> bool:
    """True only when *this symbol* has an in-flight spread open/close combo."""
    sym = symbol.lower()
    ex = getattr(conn, '_active_executor', None)
    if ex is not None:
        ex_sym = getattr(ex, 'symbol', None) or getattr(ex, '_symbol', None) or ''
        if str(ex_sym).lower() == sym:
            return True
    runtime = getattr(conn, '_runtime_state', None) or {}
    active = int(runtime.get(f'{_SPREAD_CLOSE_ACTIVE_PREFIX}:{sym}', 0) or 0)
    if active > 0:
        return True
    holder = getattr(conn, '_closing_lock_symbol', None)
    if holder and str(holder).lower() == sym:
        lock = getattr(conn, '_closing_lock', None)
        if lock is not None:
            try:
                if lock.locked():
                    return True
            except Exception:
                pass
    return False


def should_block_send(
    conn,
    symbol: str,
    config: Optional[dict] = None,
    now: Optional[datetime] = None,
) -> bool:
    """Block sends during T-1, after T-1 abort sweep, or outside trading hours."""
    if not guard_enabled(config):
        return False
    sym = (symbol or '').lower()
    if not sym:
        return False
    if get_session_phase(sym, config, now) == 't1':
        return True
    runtime = getattr(conn, '_runtime_state', None) or {}
    seg_id = segment_end_id(sym, config=config, now=now)
    if seg_id and runtime.get(f'{_T1_ABORT_PREFIX}:{sym}:{seg_id}'):
        return True
    if not is_trading_time_at(sym, now, config):
        return True
    return False


def _extract_symbol_from_instrument(instrument: str) -> str:
    try:
        from auto_connection import extract_symbol_prefix
        return (extract_symbol_prefix(instrument) or '').lower()
    except Exception:
        import re
        m = re.match(r'^([A-Za-z]+)', str(instrument or ''))
        return m.group(1).lower() if m else ''


def _exchange_pending_refs(conn, logger=None) -> Set[int]:
    try:
        orders = conn.query_orders_sync(timeout=5, use_cache=False)
    except Exception as e:
        if logger:
            logger.debug(f'[收盘守卫] 订单查询异常: {e}')
        return set()
    if not orders:
        return set()
    out: Set[int] = set()
    for o in orders:
        if str(o.get('status', '') or '') not in _PENDING_STATUS:
            continue
        try:
            ref = int(o.get('order_ref') or 0)
        except (TypeError, ValueError):
            continue
        if ref > 0:
            out.add(ref)
    return out


def _pending_refs_for_symbol(conn, symbol: str) -> List[int]:
    sym = symbol.lower()
    refs: List[int] = []
    pending = getattr(conn, 'pending_orders', None) or {}
    for ref_key, order in pending.items():
        inst = str(getattr(order, 'instrument_id', '') or '')
        if _extract_symbol_from_instrument(inst) != sym:
            continue
        try:
            ref = int(getattr(order, 'order_ref', None) or ref_key)
        except (TypeError, ValueError):
            continue
        if ref > 0:
            refs.append(ref)
    return refs


def prune_stale_pending_orders(conn, symbol: str, logger=None) -> int:
    live = _exchange_pending_refs(conn, logger)
    sym = symbol.lower()
    removed = 0
    lock = getattr(conn, 'lock', None)
    if lock is not None:
        ctx = lock
    else:
        from contextlib import nullcontext
        ctx = nullcontext()
    with ctx:
        pending = getattr(conn, 'pending_orders', None)
        if not isinstance(pending, dict):
            return 0
        for ref_key in list(pending.keys()):
            try:
                ref = int(ref_key)
            except (TypeError, ValueError):
                ref = int(getattr(pending[ref_key], 'order_ref', 0) or 0)
            order = pending[ref_key]
            inst = str(getattr(order, 'instrument_id', '') or '')
            if _extract_symbol_from_instrument(inst) != sym:
                continue
            if ref in live:
                continue
            pending.pop(ref_key, None)
            traded = getattr(conn, 'order_traded', None)
            if isinstance(traded, dict):
                traded.pop(ref, None)
                traded.pop(ref_key, None)
            removed += 1
    if removed and logger:
        logger.info(f'[收盘守卫] {symbol} 同步移除 {removed} 条本地幽灵在途')
    return removed


def _cancel_symbol_pending(conn, symbol: str, logger, config: dict) -> int:
    refs = _pending_refs_for_symbol(conn, symbol)
    if not refs:
        prune_stale_pending_orders(conn, symbol, logger)
        return 0
    try:
        count = conn.cancel_all_pending_orders(
            timeout=float(config.get('CANCEL_ALL_TIMEOUT', 5) or 5),
        )
    except Exception as e:
        if logger:
            logger.warning(f'[收盘守卫] {symbol} T-1 撤单失败: {e}')
        return 0
    prune_stale_pending_orders(conn, symbol, logger)
    return int(count or 0)


def maybe_run_pre_close_cancel_sweep(
    conn,
    tradeinfo: List[dict],
    config: dict,
    logger,
) -> None:
    if not guard_enabled(config):
        return
    if not getattr(conn, 'td_logined', False):
        return

    runtime = getattr(conn, '_runtime_state', None)
    if runtime is None:
        return

    seen: Set[str] = set()
    for item in tradeinfo or []:
        symbol = (item.get('future') or '').strip()
        if not symbol:
            continue
        sym = symbol.lower()
        if sym in seen:
            continue
        seen.add(sym)

        if not is_trading_time_at(symbol, config=config):
            continue
        if get_session_phase(symbol, config) != 't1':
            continue

        seg_id = segment_end_id(symbol, config=config)
        if not seg_id:
            continue
        dedupe_key = f'{_T1_CANCEL_PREFIX}:{sym}:{seg_id}'
        if runtime.get(dedupe_key):
            continue

        count = _cancel_symbol_pending(conn, symbol, logger, config)
        runtime[dedupe_key] = time.time()
        runtime[f'{_T1_ABORT_PREFIX}:{sym}:{seg_id}'] = time.time()
        sec = seconds_to_segment_end(symbol, config=config)
        if logger:
            logger.warning(
                f'[收盘守卫] {symbol} 小节 T-{int(sec or 0)}s：'
                f'T-1 硬停，已撤在途 {count} 笔'
            )


def should_skip_strangle_rebalance(
    conn,
    config: dict,
    ledger=None,
    tradeinfo=None,
) -> Tuple[bool, str]:
    if not guard_enabled(config):
        return False, ''
    symbols: Set[str] = set()
    for item in tradeinfo or []:
        sym = (item.get('future') or '').lower()
        if sym:
            symbols.add(sym)
    if ledger is not None:
        for item in ledger.list_unmatched_legs() or []:
            sym = (item.get('symbol') or '').lower()
            if sym:
                symbols.add(sym)
    for sym in symbols:
        phase = get_session_phase(sym, config)
        if phase == 't1':
            return True, f'{sym} 处于 T-1'
        if phase == 'off':
            return True, f'{sym} 非交易时段'
    return False, ''


def install_session_close_guard(config: dict = None) -> bool:
    global _INSTALLED, _INSTALL_ERROR
    if _INSTALLED:
        _INSTALL_ERROR = None
        return True

    cfg = config or {}
    if not guard_enabled(cfg):
        return True

    try:
        _install_send_order_guard()
        _install_close_executor_guard()
        _install_spread_guards()
        _install_strangle_guards()
    except Exception as e:
        _INSTALL_ERROR = repr(e)
        _log.error('[收盘守卫] 安装失败: %s', e, exc_info=True)
        return False

    _INSTALLED = True
    _INSTALL_ERROR = None
    return True


def _install_send_order_guard() -> None:
    import auto_order_manager as aom

    original = aom.OrderManager.send_order
    if getattr(original, '_session_close_wrapped', False):
        return

    def guarded_send_order(self, instrument, *args, **kwargs):
        conn = self.conn
        config = getattr(conn, 'config', None) or {}
        sym = _extract_symbol_from_instrument(instrument)
        if sym and should_block_send(conn, sym, config):
            _maybe_log_throttled(
                conn, f't1_send:{sym}', self.logger, 'info',
                f'[收盘守卫] {sym} 禁止发单: {instrument}',
            )
            return None, None
        return original(self, instrument, *args, **kwargs)

    guarded_send_order._session_close_wrapped = True  # type: ignore[attr-defined]
    aom.OrderManager.send_order = guarded_send_order


def _install_close_executor_guard() -> None:
    try:
        import auto_closer_executor as ace
    except ImportError:
        _log.info('[收盘守卫] auto_closer_executor 不可用，跳过平仓发单补丁')
        return

    orig_send = ace._send_and_wait
    if not getattr(orig_send, '_session_close_wrapped', False):

        def guarded_send_and_wait(
            conn, instrument, direction, volume, price, offset,
            timeout, config, logger, symbol,
            base_future_price: float = None,
            price_change_threshold: float = None,
            strategy: str = 'spread',
            **kwargs,
        ):
            if should_block_send(conn, symbol, config):
                _maybe_log_throttled(
                    conn, f't1_send:{symbol.lower()}', logger, 'info',
                    f'[收盘守卫] {symbol} 禁止发单: {instrument}',
                )
                return False, 0, 0.0
            return orig_send(
                conn, instrument, direction, volume, price, offset,
                timeout, config, logger, symbol,
                base_future_price=base_future_price,
                price_change_threshold=price_change_threshold,
                strategy=strategy,
                **kwargs,
            )

        guarded_send_and_wait._session_close_wrapped = True  # type: ignore[attr-defined]
        ace._send_and_wait = guarded_send_and_wait

    orig_close_leg = ace._close_single_leg
    if not getattr(orig_close_leg, '_session_close_wrapped', False):

        def guarded_close_single_leg(
            conn, contract, direction, volume, price, min_tick, config, logger, symbol,
            timeout_multiplier: float = 1.0, leg: str = '',
            group_retry_count: int = 1,
            base_future_price: float = None,
            price_change_threshold: float = None,
            strategy: str = 'spread',
            **kwargs,
        ):
            if should_block_send(conn, symbol, config):
                _maybe_log_throttled(
                    conn, f'off_close_leg:{symbol.lower()}', logger, 'info',
                    f'[收盘守卫] {symbol} 非交易/T-1，跳过单腿平仓: {contract}',
                )
                return False, 0, 0.0
            return orig_close_leg(
                conn, contract, direction, volume, price, min_tick, config, logger, symbol,
                timeout_multiplier=timeout_multiplier,
                leg=leg,
                group_retry_count=group_retry_count,
                base_future_price=base_future_price,
                price_change_threshold=price_change_threshold,
                strategy=strategy,
                **kwargs,
            )

        guarded_close_single_leg._session_close_wrapped = True  # type: ignore[attr-defined]
        ace._close_single_leg = guarded_close_single_leg

    orig_execute = ace.execute_close_orders_with_limit
    if getattr(orig_execute, '_session_close_wrapped', False):
        return

    def guarded_execute_close(conn, plan, symbol, month, min_tick, config, logger,
                              urgency='urgent'):
        sym = (symbol or '').lower()
        runtime = getattr(conn, '_runtime_state', None)
        if runtime is None:
            runtime = {}
            conn._runtime_state = runtime
        active_key = f'{_SPREAD_CLOSE_ACTIVE_PREFIX}:{sym}'
        runtime[active_key] = int(runtime.get(active_key, 0) or 0) + 1
        conn._closing_lock_symbol = sym
        try:
            return orig_execute(
                conn, plan, symbol, month, min_tick, config, logger,
                urgency=urgency,
            )
        finally:
            runtime[active_key] = max(0, int(runtime.get(active_key, 1) or 1) - 1)
            if runtime.get(active_key, 0) <= 0:
                runtime.pop(active_key, None)
            if getattr(conn, '_closing_lock_symbol', None) == sym:
                try:
                    delattr(conn, '_closing_lock_symbol')
                except Exception:
                    conn._closing_lock_symbol = None

    guarded_execute_close._session_close_wrapped = True  # type: ignore[attr-defined]
    ace.execute_close_orders_with_limit = guarded_execute_close


def _patch_once(fn, patched):
    if getattr(fn, '_session_close_wrapped', False):
        return fn
    patched._session_close_wrapped = True  # type: ignore[attr-defined]
    return patched


def _install_spread_guards() -> None:
    import auto_processor
    import auto_closer

    orig_process = auto_processor.process_symbol

    def patched_process_symbol(
        conn, item, vix_engine, config, logger, remaining_limit=None,
    ):
        symbol = item['future']
        if get_session_phase(symbol, config) == 't1':
            _maybe_log_throttled(
                conn, f't1_spread:{symbol.lower()}', logger, 'info',
                f'[{symbol}] 收盘 T-1 硬停，跳过价差扫描',
            )
            return False
        if not is_trading_time_at(symbol, config=config):
            return False
        return orig_process(
            conn, item, vix_engine, config, logger,
            remaining_limit=remaining_limit,
        )

    auto_processor.process_symbol = _patch_once(orig_process, patched_process_symbol)

    orig_close = auto_closer.process_close

    def patched_process_close(conn, item, vix, config, logger, positions=None):
        symbol = item['future']
        phase = get_session_phase(symbol, config)
        if phase == 't1':
            return False
        if not is_trading_time_at(symbol, config=config):
            _maybe_log_throttled(
                conn, f'off_close:{symbol.lower()}', logger, 'info',
                f'[{symbol}] 非交易时段，跳过价差平仓',
            )
            return False
        if phase == 't10' and not spread_combo_in_progress(conn, symbol):
            _maybe_log_throttled(
                conn, f't10_close:{symbol.lower()}', logger, 'info',
                f'[{symbol}] 收盘 T-10：禁止新发起价差平仓',
            )
            return False
        return orig_close(conn, item, vix, config, logger, positions=positions)

    auto_closer.process_close = _patch_once(orig_close, patched_process_close)
    if getattr(auto_processor, 'process_close', None) is orig_close:
        auto_processor.process_close = auto_closer.process_close

    try:
        import auto_executor as ae
        cls = ae.SymbolTradeExecutor

        def _wrap_exec(meth_name: str):
            orig = getattr(cls, meth_name)

            def patched(self, *a, **kw):
                sym = getattr(self, 'symbol', '') or getattr(self, '_symbol', '')
                config = getattr(self, 'config', None) or getattr(
                    getattr(self, 'conn', None), 'config', None,
                )
                phase = get_session_phase(sym, config) if sym else 'normal'
                if phase == 't10' and not spread_combo_in_progress(self.conn, sym):
                    _maybe_log_throttled(
                        self.conn, f't10_open:{str(sym).lower()}',
                        getattr(self, 'logger', None), 'info',
                        f'[{sym}] 收盘 T-10：禁止开新价差组',
                    )
                    return 0
                if phase == 't1':
                    return 0
                return orig(self, *a, **kw)

            return _patch_once(orig, patched)

        for name in ('execute_all_combos', 'execute_single_open_clip'):
            if hasattr(cls, name):
                setattr(cls, name, _wrap_exec(name))
    except Exception as e:
        _log.warning('[收盘守卫] 价差 executor 补丁跳过: %s', e)


def _install_strangle_guards() -> None:
    try:
        import straggle_processor as sp
        import straggle_execution as se
    except ImportError:
        _log.info('[收盘守卫] autostraggle 不可用，跳过宽跨补丁')
        return

    orig_ps = sp.process_strangle_symbol

    def patched_process_strangle_symbol(
        conn, item, vix_engine, config, logger,
        ledger, executor, circuit_breaker=None,
        allow_quarantine_close_only=False,
    ):
        symbol = item['future']
        if get_session_phase(symbol, config) == 't1':
            _maybe_log_throttled(
                conn, f't1_strangle:{symbol.lower()}', logger, 'info',
                f'[{symbol}] 收盘 T-1 硬停，跳过宽跨扫描',
            )
            return False
        if not is_trading_time_at(symbol, config=config):
            _maybe_log_throttled(
                conn, f'off_strangle:{symbol.lower()}', logger, 'info',
                f'[{symbol}] 非交易时段，跳过宽跨扫描',
            )
            return False
        return orig_ps(
            conn, item, vix_engine, config, logger,
            ledger, executor, circuit_breaker=circuit_breaker,
            allow_quarantine_close_only=allow_quarantine_close_only,
        )

    sp.process_strangle_symbol = _patch_once(orig_ps, patched_process_strangle_symbol)

    cls = se.StrangleExecutor
    if hasattr(cls, 'execute_open'):
        orig_open = cls.execute_open

        def patched_execute_open(self, item, *a, **kw):
            symbol = item['future']
            config = getattr(self.conn, 'config', None) or {}
            phase = get_session_phase(symbol, config)
            if phase == 't10':
                _maybe_log_throttled(
                    self.conn, f't10_st_open:{symbol.lower()}',
                    getattr(self, 'logger', None), 'info',
                    f'[{symbol}] 收盘 T-10：禁止宽跨新组开仓',
                )
                return False
            if phase == 't1':
                return False
            return orig_open(self, item, *a, **kw)

        cls.execute_open = _patch_once(orig_open, patched_execute_open)

    if hasattr(cls, 'execute_close'):
        orig_close = cls.execute_close

        def patched_execute_close(self, position, item, *, urgent: bool = False):
            symbol = item['future']
            config = getattr(self.conn, 'config', None) or {}
            phase = get_session_phase(symbol, config)
            if phase == 't1':
                return False
            if phase == 't10' and (position or {}).get('status') == 'open':
                _maybe_log_throttled(
                    self.conn, f't10_st_close:{symbol.lower()}',
                    getattr(self, 'logger', None), 'info',
                    f'[{symbol}] 收盘 T-10：禁止宽跨新组平仓',
                )
                return False
            return orig_close(self, position, item, urgent=urgent)

        cls.execute_close = _patch_once(orig_close, patched_execute_close)
