"""Make ``HealthChecker`` zombie-order auto-cancel safer.

Original behavior (auto_health_check.HealthChecker.check_now with
``auto_fix=True``): any local ``pending_orders`` entry older than 5 minutes
triggers ``cancel_all_pending_orders``. Two issues in real markets:

1. **CTP回报延迟**: a peak-time queueing window can keep an order in the
   local map far past 5 min while it is still validly working at the
   exchange. Auto-cancel here is unnecessary.
2. **反复触发**: each main-loop tick that sees the same zombie may issue a
   new ``cancel_all_pending_orders`` (which itself is global, not per-ref),
   wasting RPC budget and burning rate-limit headroom.

We therefore:

* Skip the auto-cancel unless an exchange-side query confirms the ref is
  *still* in a non-terminal state.
* Track per-ref last cancel time in ``conn._runtime_state['_zombie_cancel_at']``
  and refuse re-cancellation within ``zombie_cancel_cooldown_sec`` (default
  300s) for the same ref.

The patch wraps the original method so all the report bookkeeping is
preserved unchanged.
"""

from __future__ import annotations

import time
from typing import Optional

_INSTALLED = False
_INSTALL_ERROR: Optional[str] = None


def is_installed() -> bool:
    """Return True when the health check wrapper is currently active."""
    return _INSTALLED


def get_install_error() -> Optional[str]:
    """Return last install failure reason, or None when patch is installed."""
    return _INSTALL_ERROR

# Same pending-status set as ctp_recovery_patch.
_PENDING_STATUS = frozenset({'1', '3', 'a', 'b', 'c'})


def _conf(conn, key, default):
    cfg = getattr(conn, 'config', None) or {}
    try:
        v = cfg.get(key, default)
    except AttributeError:
        return default
    return default if v is None else v


def _exchange_pending_refs(conn, logger) -> set:
    """Return order_refs still pending at the exchange (best effort)."""
    try:
        orders = conn.query_orders_sync(timeout=5, use_cache=False)
    except Exception as e:
        if logger:
            logger.debug(f'[健康检查] 僵尸订单核对查询异常: {e}')
        return set()
    if orders is None:
        return set()
    out = set()
    for o in orders:
        status = str(o.get('status', '') or '')
        if status not in _PENDING_STATUS:
            continue
        try:
            ref = int(o.get('order_ref') or 0)
        except (TypeError, ValueError):
            continue
        if ref > 0:
            out.add(ref)
    return out


def install_health_check_patch() -> bool:
    """Replace ``HealthChecker.check_now`` with a safer auto-cancel path.

    Returns True when the patch is now active, False when installation failed.
    Inspect :func:`get_install_error` for the reason.
    """
    global _INSTALLED, _INSTALL_ERROR
    if _INSTALLED:
        return True

    try:
        import auto_health_check as ahc
    except Exception as e:
        _INSTALL_ERROR = f'import auto_health_check 失败: {e}'
        return False

    HealthChecker = getattr(ahc, 'HealthChecker', None)
    if HealthChecker is None:
        _INSTALL_ERROR = (
            'auto_health_check.HealthChecker 未找到（autotrade 版本不兼容？）'
        )
        return False

    original = HealthChecker.check_now

    def _session_suspended_stub():
        return {
            'healthy': True,
            'issues': [],
            'details': {'expected_suspend_offline': True},
        }

    def patched_check_now(self, force: bool = False, auto_fix: bool = True):
        try:
            from auto_scheduled_pause import is_connection_suspended

            cfg = getattr(self.conn, 'config', None) or {}
            if is_connection_suspended(cfg):
                return _session_suspended_stub()
        except Exception:
            pass

        # Run the original inspection but with auto_fix forced off so it does
        # not issue the broad cancel; we then decide ourselves whether to
        # cancel based on exchange-confirmed pending refs and per-ref cooldown.
        report = original(self, force=force, auto_fix=False)

        if not auto_fix or not report:
            return report

        zombie_orders = []
        for p in report.get('details', {}).get('pending_list', []) or []:
            try:
                if float(p.get('age', 0)) > 300:
                    zombie_orders.append(p)
            except (TypeError, ValueError):
                continue

        if not zombie_orders:
            return report

        conn = self.conn
        runtime = getattr(conn, '_runtime_state', None)
        if runtime is None:
            return report

        cooldown = float(_conf(conn, 'zombie_cancel_cooldown_sec', 300))
        cancel_at = runtime.setdefault('_zombie_cancel_at', {})
        now = time.time()
        # Prune entries older than cooldown.
        for k in list(cancel_at.keys()):
            try:
                if now - float(cancel_at[k]) > max(cooldown * 2, 600):
                    cancel_at.pop(k, None)
            except (TypeError, ValueError):
                cancel_at.pop(k, None)

        candidate_refs = []
        for p in zombie_orders:
            try:
                ref = int(p.get('ref') or 0)
            except (TypeError, ValueError):
                continue
            if ref <= 0:
                continue
            last = float(cancel_at.get(ref, 0.0))
            if now - last < cooldown:
                continue
            candidate_refs.append(ref)

        if not candidate_refs:
            return report

        # Confirm with the exchange that the candidate refs are STILL pending
        # before issuing any cancel.
        live_refs = _exchange_pending_refs(conn, self.logger)
        confirmed = [r for r in candidate_refs if r in live_refs]
        if not confirmed:
            self.logger.info(
                f'[健康检查] 候选僵尸 {len(candidate_refs)} 个但交易所均已终结，'
                '不发起撤单'
            )
            for r in candidate_refs:
                cancel_at[r] = now  # avoid hot loop next round
            return report

        try:
            cancel_count = self.conn.cancel_all_pending_orders(timeout=5)
            for r in confirmed:
                cancel_at[r] = now
            if cancel_count > 0:
                self.logger.warning(
                    f'[健康检查] 自动撤销 {cancel_count} 个僵尸订单'
                    f'（交易所确认在途 {len(confirmed)} 个）'
                )
            else:
                self.logger.info(
                    '[健康检查] cancel_all_pending_orders 返回 0，'
                    '可能交易所端已自动终结'
                )
        except Exception as e:
            self.logger.error(f'[健康检查] 自动撤销僵尸订单失败: {e}')

        return report

    HealthChecker.check_now = patched_check_now
    _INSTALLED = True
    _INSTALL_ERROR = None
    return True
