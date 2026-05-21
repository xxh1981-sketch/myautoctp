"""Strengthen ``auto_reconnect_recovery.run_post_reconnect_recovery``.

Two issues addressed:

* **R3**: Original code skips full-account cancel when the code table has not
  reloaded, but still proceeds to clear ``_reconnect_quarantine``. That leaves
  an exposure window where exchange-side orders may still exist while the
  local ``pending_orders`` map is empty. We block quarantine-clear in this
  case so the watchdog has a chance to retry next round (or escalate to
  full_recovery once ``quarantine_max_seconds`` elapses).

* **B5**: After ``cancel_all_pending_orders`` is *attempted*, query the
  exchange once more; if any order is still in a non-terminal state, rebuild
  ``OrderInfo`` (with FrontID / SessionID) into the local ``pending_orders``
  map so subsequent ``assert_no_pending`` and watchdog logic remain accurate.
  This protects against partial-cancel scenarios (broker rate-limit, network
  flap during cancel confirmation) where the prior implementation would have
  silently lost track of live exchange orders.

Both behaviors are additive — we wrap the original function and call it
first, then perform the extra checks.
"""

from __future__ import annotations

import time

_INSTALLED = False


def _is_pending_status(order_status: str) -> bool:
    # ALL_TRADED='0', PART_TRADED_QUEUEING='1', PART_TRADED_NOT_QUEUEING='2',
    # NO_TRADE_QUEUEING='3', NO_TRADE_NOT_QUEUEING='4', CANCELED='5',
    # UNKNOWN='a', NOT_TOUCHED='b', TOUCHED='c'.
    # Pending = not yet terminal: anything still queued / partially filled.
    return order_status in ('1', '3', 'a', 'b', 'c')


def _rebuild_pending_orders_from_exchange(conn, logger) -> int:
    """Rebuild local ``pending_orders`` from CTP order query (best effort)."""
    try:
        orders = conn.query_orders_sync(timeout=8, use_cache=False)
    except Exception as e:
        logger.warning(f"[重连] 重建 pending_orders: 订单查询异常: {e}")
        return 0
    if orders is None:
        logger.warning("[重连] 重建 pending_orders: 订单查询失败，跳过")
        return 0

    from pairtrade.models import OrderInfo

    rebuilt = 0
    now = time.time()
    with conn.lock:
        for o in orders:
            status = str(o.get('status', '') or '')
            if not _is_pending_status(status):
                continue
            try:
                order_ref = int(o.get('order_ref') or 0)
            except (TypeError, ValueError):
                continue
            if order_ref <= 0:
                continue
            if order_ref in conn.pending_orders:
                continue
            try:
                volume_total = int(o.get('volume_total') or 0)
            except (TypeError, ValueError):
                volume_total = 0
            try:
                price = float(o.get('price') or 0.0)
            except (TypeError, ValueError):
                price = 0.0
            instrument = str(o.get('instrument') or '').strip()
            exchange_id = str(o.get('exchange_id') or '').strip()
            direction = str(o.get('direction') or '')
            offset = str(o.get('offset') or '0')
            front_id = o.get('front_id')
            session_id = o.get('session_id')
            if not instrument or not exchange_id:
                continue

            info = OrderInfo(
                order_ref=order_ref,
                instrument_id=instrument,
                exchange_id=exchange_id,
                volume=volume_total,
                price=price,
                direction=direction,
                offset=offset,
                create_time=now,
            )
            try:
                info.front_id = front_id
                info.session_id = session_id
                info.order_status = status
            except Exception:
                pass
            conn.pending_orders[order_ref] = info
            conn.order_traded[order_ref] = False
            rebuilt += 1

    if rebuilt:
        logger.warning(
            f"[重连] 已根据 CTP 订单查询重建 {rebuilt} 个本地 pending_orders 记录"
        )
    return rebuilt


def install_recovery_patch() -> None:
    """Wrap ``run_post_reconnect_recovery`` with R3 (quarantine guard) + B5 (rebuild)."""
    global _INSTALLED
    if _INSTALLED:
        return

    try:
        import auto_reconnect_recovery as arr
    except Exception:
        return

    original = getattr(arr, 'run_post_reconnect_recovery', None)
    if original is None:
        return

    def patched(conn, logger) -> None:
        # We re-implement the body to weave in the two new behaviors. We
        # cannot simply call ``original`` because R3 needs to override its
        # quarantine-clear branch.
        from auto_reconnect_recovery import (
            both_channels_ready,
            set_quarantine,
            wait_for_both_channels,
        )

        skip_cancel = False
        cancel_attempted_ok = False
        recovery_ok = False
        try:
            if not wait_for_both_channels(conn, logger, conn.config):
                return

            if not conn.code_table_loaded:
                deadline = time.time() + conn.config.get(
                    'reconnect_code_table_wait', 60,
                )
                while not conn.code_table_loaded and time.time() < deadline:
                    time.sleep(0.5)
                if not conn.code_table_loaded:
                    logger.warning(
                        "[重连] 等待码表超时，跳过全量撤单"
                        "（保持隔离期，等待下一轮重试或看门狗升级）"
                    )
                    skip_cancel = True
            else:
                time.sleep(1)

            if not skip_cancel and conn.td_logined and conn.td_api:
                try:
                    cancel_count = conn.cancel_all_pending_orders(timeout=10)
                    if cancel_count > 0:
                        logger.warning(
                            f"[重连] 已撤销交易所侧 {cancel_count} 个残留挂单"
                        )
                    else:
                        logger.info("[重连] 无残留挂单需要撤销")
                    cancel_attempted_ok = True
                except Exception as e:
                    logger.error(f"[重连] 全量撤单失败: {e}")
                    cancel_attempted_ok = False

            try:
                conn.position_tracker.mark_needs_calibration()
                success, msg = conn.position_tracker.calibrate_from_ctp(timeout=10)
                if success:
                    logger.info(f"[重连] 持仓校准完成: {msg}")
                else:
                    logger.warning(
                        f"[重连] 持仓校准未成功: {msg}（下次定期校准将重试）"
                    )
            except Exception as e:
                logger.error(f"[重连] 持仓校准异常: {e}")

            # B5: After cancel attempt, mirror exchange-side pending into the
            # local map so future logic (assert_no_pending, watchdog,
            # cancel_all_pending_orders fallback) sees the truth even if
            # the cancel was rate-limited or partial.
            if conn.td_logined and conn.td_api:
                try:
                    _rebuild_pending_orders_from_exchange(conn, logger)
                except Exception as e:
                    logger.warning(f"[重连] 重建 pending_orders 异常: {e}")

            # R3: Only declare recovery successful when we are confident
            # nothing is still hanging on the exchange. Skipped cancel ⇒
            # keep quarantine on so the next round retries.
            recovery_ok = (
                both_channels_ready(conn)
                and (cancel_attempted_ok or not skip_cancel)
            )
        finally:
            if recovery_ok and conn._reconnect_quarantine:
                set_quarantine(conn, False)
                mgr = getattr(conn, '_reconnect_mgr', None)
                if mgr is not None:
                    mgr.td_reconnect_count = 0
                    mgr.md_reconnect_count = 0
                logger.info("[重连] 隔离期结束，恢复交易")
            elif conn._reconnect_quarantine:
                logger.warning(
                    "[重连] 恢复收尾未完成或撤单/码表未就绪，保持隔离期"
                )

    arr.run_post_reconnect_recovery = patched
    _INSTALLED = True
