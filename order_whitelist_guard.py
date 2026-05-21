"""Tighten ``send_order`` to a ``(symbol, month)`` allowlist (AutoCTP-side patch).

``auto_order_manager.OrderManager.send_order`` already rejects instruments
whose symbol is not in ``conn.symbols``. That is the *first* line of defense
but it does not protect against:

* CSV / ledger tampering pointing to a neighbouring month (e.g. SA2608 when
  tradeinfo only targets SA2607). The symbol still matches so the legacy
  guard waves it through.
* Logic bugs that accidentally hand a future contract (``SA2607``) to the
  order layer.

We install a one-time wrapper that:

* extracts the instrument month via ``extract_month_from_contract``;
* compares it (raw + ``_normalize_month``) against ``conn.target_months``;
* rejects the request and fires a feishu alert on mismatch;
* also rejects "no-month" instruments (futures contracts), because the
  dual-strategy program is option-only.

The wrapper is *additive* — it runs before the original ``send_order`` and
delegates to it on pass.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

_log = logging.getLogger(__name__)

_INSTALLED = False
_INSTALL_ERROR: Optional[str] = None
_DEFAULT_WHITELIST_FEISHU_COOLDOWN_SEC = 300
_ALERT_TS_PREFIX = '_whitelist_feishu_alert_at'


def is_installed() -> bool:
    """Return True when the guard wrapper is currently active."""
    return _INSTALLED


def get_install_error() -> Optional[str]:
    """Return last install failure reason, or None when guard is installed."""
    return _INSTALL_ERROR


def _alert_cooldown_sec(config: Optional[dict]) -> float:
    if not config:
        return float(_DEFAULT_WHITELIST_FEISHU_COOLDOWN_SEC)
    try:
        v = config.get(
            'whitelist_feishu_cooldown_sec',
            _DEFAULT_WHITELIST_FEISHU_COOLDOWN_SEC,
        )
    except AttributeError:
        return float(_DEFAULT_WHITELIST_FEISHU_COOLDOWN_SEC)
    if v is None:
        return float(_DEFAULT_WHITELIST_FEISHU_COOLDOWN_SEC)
    return float(v)


def _should_send_feishu(conn, alert_key: str, config: Optional[dict]) -> bool:
    """Per (conn, alert_key) cooldown — log every reject, feishu at most once per window."""
    if not alert_key:
        return True
    runtime = getattr(conn, '_runtime_state', None)
    if runtime is None:
        runtime = {}
        try:
            conn._runtime_state = runtime
        except Exception:
            return True

    cooldown = _alert_cooldown_sec(config)
    if cooldown <= 0:
        return True

    now = time.time()
    state_key = f'{_ALERT_TS_PREFIX}:{alert_key}'
    last = float(runtime.get(state_key) or 0.0)
    if now - last < cooldown:
        return False
    runtime[state_key] = now
    return True


def _alert(
    message: str,
    config: Optional[dict] = None,
    conn=None,
    alert_key: str = '',
) -> None:
    if conn is not None and not _should_send_feishu(conn, alert_key, config):
        _log.debug('[发单白名单] 飞书告警冷却中，跳过: %s', alert_key)
        return
    try:
        from auto_feishu import send_feishu_message
        send_feishu_message(message, config=config)
    except Exception as e:
        _log.warning('飞书告警发送失败: %s', e, exc_info=True)


def _resolve_instrument_month(conn, sym: str, instrument: str) -> Optional[str]:
    """Return contract month (raw, e.g. '2608') or None when not extractable."""
    try:
        from auto_connection_utils import extract_month_from_contract
    except Exception:
        return None
    return extract_month_from_contract(instrument)


def _target_month_set(conn, sym: str) -> set:
    raw = getattr(conn, 'target_months', None) or {}
    months = raw.get(sym) or raw.get(sym.lower()) or raw.get(sym.upper()) or []
    if isinstance(months, str):
        months = [months]
    out: set = set()
    for m in months:
        m_str = str(m).strip()
        if not m_str:
            continue
        out.add(m_str)
        try:
            out.add(conn._normalize_month(sym, m_str))
        except Exception:
            pass
    return out


def audit_target_months_coverage(
    conn,
    spread_tradeinfo: list,
    strangle_tradeinfo: list,
) -> list[str]:
    """Return lower-case symbols from tradeinfo with empty ``conn.target_months``.

    Used at startup to warn when the month whitelist guard cannot block
    neighbouring-month orders for those symbols.
    """
    missing: list[str] = []
    seen: set[str] = set()
    for item in (spread_tradeinfo or []) + (strangle_tradeinfo or []):
        sym = (item.get('future') or '').lower()
        if not sym or sym in seen:
            continue
        seen.add(sym)
        if not _target_month_set(conn, sym):
            missing.append(sym)
    return sorted(missing)


def _is_option_like(instrument: str) -> bool:
    """Heuristic: option contracts contain a C/P strike marker; futures do not."""
    import re

    return bool(re.search(r'[-]?[CP][-]?\d', (instrument or '').upper()))


def install_send_order_month_guard() -> bool:
    """Replace ``OrderManager.send_order`` with a (sym, month) gated wrapper.

    Returns:
        ``True`` if the guard is active (just installed, or already installed),
        ``False`` if installation failed silently. Callers (e.g. ``merged_main``)
        must treat a ``False`` result as a critical defect — without this guard,
        邻月错单仅靠 autotrade 品种级检查，对 CSV/账本被篡改场景无防护。
        Inspect :func:`get_install_error` for the reason.
    """
    global _INSTALLED, _INSTALL_ERROR
    if _INSTALLED:
        _INSTALL_ERROR = None
        return True

    try:
        import auto_order_manager as aom
    except Exception as e:
        _INSTALL_ERROR = f'import auto_order_manager 失败: {e}'
        _log.error('[发单白名单] %s', _INSTALL_ERROR)
        return False

    OrderManager = getattr(aom, 'OrderManager', None)
    if OrderManager is None:
        _INSTALL_ERROR = 'auto_order_manager.OrderManager 不存在'
        _log.error('[发单白名单] %s', _INSTALL_ERROR)
        return False

    original = OrderManager.send_order

    def guarded_send_order(
        self,
        instrument,
        direction,
        volume,
        price,
        offset='0',
        hedge='1',
        assert_no_pending: bool = False,
        strategy: str = 'spread',
    ):
        conn = self.conn
        cfg = getattr(conn, 'config', None)

        try:
            price_f = float(price)
        except (TypeError, ValueError):
            price_f = 0.0
        try:
            volume_i = int(volume)
        except (TypeError, ValueError):
            volume_i = 0
        if price_f <= 0 or volume_i <= 0:
            msg = (
                f'[发单白名单] 拒绝非法参数下单: {instrument} '
                f'(price={price}, volume={volume}, strategy={strategy})'
            )
            self.logger.error(msg)
            sym = ''
            try:
                from auto_connection import extract_symbol_prefix
                sym = extract_symbol_prefix(instrument) or ''
            except Exception:
                pass
            _alert(
                '⚠️ **发单白名单拦截**\n\n' + msg,
                config=cfg,
                conn=conn,
                alert_key=f'invalid_params:{sym.lower()}',
            )
            return None, None

        if not _is_option_like(instrument):
            msg = (
                f'[发单白名单] 拒绝非期权合约下单: {instrument} '
                f'(strategy={strategy}, 双策略程序仅交易期权)'
            )
            self.logger.error(msg)
            sym = ''
            try:
                from auto_connection import extract_symbol_prefix
                sym = extract_symbol_prefix(instrument) or ''
            except Exception:
                pass
            _alert(
                '⚠️ **发单白名单拦截**\n\n' + msg,
                config=cfg,
                conn=conn,
                alert_key=f'not_option:{sym.lower()}',
            )
            return None, None

        from auto_connection import extract_symbol_prefix

        sym = extract_symbol_prefix(instrument)
        contract_month = _resolve_instrument_month(conn, sym, instrument)
        target_months = _target_month_set(conn, sym)

        if not contract_month:
            msg = (
                f'[发单白名单] 拒绝下单: 无法解析月份 {instrument} '
                f'(sym={sym}, strategy={strategy})'
            )
            self.logger.error(msg)
            _alert(
                '⚠️ **发单白名单拦截**\n\n' + msg,
                config=cfg,
                conn=conn,
                alert_key=f'no_month:{sym.lower()}',
            )
            return None, None

        if target_months and contract_month not in target_months:
            try:
                normalized = conn._normalize_month(sym, contract_month)
            except Exception:
                normalized = contract_month
            if normalized not in target_months:
                msg = (
                    f'[发单白名单] 拒绝非目标月份下单: {instrument} '
                    f'(sym={sym} month={contract_month} '
                    f'目标月份={sorted(target_months)} strategy={strategy})'
                )
                self.logger.error(msg)
                _alert(
                    '⚠️ **发单白名单拦截**\n\n' + msg,
                    config=cfg,
                    conn=conn,
                    alert_key=f'wrong_month:{sym.lower()}:{contract_month}',
                )
                return None, None

        return original(
            self, instrument, direction, volume, price, offset, hedge,
            assert_no_pending, strategy=strategy,
        )

    OrderManager.send_order = guarded_send_order
    _INSTALLED = True
    _INSTALL_ERROR = None
    return True
