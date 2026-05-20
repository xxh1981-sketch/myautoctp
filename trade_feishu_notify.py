"""Unified Feishu message format for all trade fills."""

from __future__ import annotations

import threading
from typing import Dict

FILL_SIDE_LABELS = {
    'buy_open': 'Buy Open / 买开',
    'sell_open': 'Sell Open / 卖开',
    'buy_close': 'Buy Close / 买平',
    'sell_close': 'Sell Close / 卖平',
}

STRATEGY_LABELS = {
    'spread': 'Spread / 价差',
    'strangle': 'Strangle / 宽跨',
    'other': 'Other / 其他',
}


def _format_time(trade: dict) -> str:
    td = (trade.get('trade_date') or '').strip()
    tt = (trade.get('trade_time') or '').strip()
    if td and tt:
        if len(td) == 8 and td.isdigit():
            td = f'{td[:4]}-{td[4:6]}-{td[6:8]}'
        return f'{td} {tt}'
    if tt:
        return tt
    import time
    return time.strftime('%Y-%m-%d %H:%M:%S')


def format_fill_feishu_message(row: Dict[str, str], trade: dict = None) -> str:
    """Build a single friendly Feishu text for one fill."""
    trade = trade or {}
    side = FILL_SIDE_LABELS.get(row.get('fill_side', ''), row.get('fill_side', ''))
    strategy = STRATEGY_LABELS.get(row.get('strategy', ''), row.get('strategy', ''))
    when = _format_time(trade)

    lines = [
        '✅ **Fill Report / 成交回报**',
        '',
        f"**Instrument / 合约**  {row.get('instrument_code', '')}",
        f"**Side / 方向**  {side}",
        f"**Strategy / 策略**  {strategy}",
        f"**Volume / 数量**  {row.get('fill_volume', '')} lots",
        f"**Fill Price / 成交价**  {row.get('fill_price', '')}",
    ]

    bid = row.get('bid_price') or ''
    ask = row.get('ask_price') or ''
    if bid and ask:
        lines.append(f"**Bid / Ask / 买一·卖一**  {bid} / {ask}")
        slip = row.get('slippage_vs_mid') or ''
        if slip:
            try:
                slip_f = float(slip)
                sign = '+' if slip_f >= 0 else ''
                mid = (float(bid) + float(ask)) / 2.0
                lines.append(
                    f"**Slippage / 滑点**  {sign}{slip} vs mid {mid:.4f}"
                )
            except (TypeError, ValueError):
                lines.append(f"**Slippage / 滑点**  {slip}")
    else:
        lines.append('**Bid / Ask / 买一·卖一**  —')

    order_ref = trade.get('order_ref')
    if order_ref:
        lines.append(f"**OrderRef**  {order_ref}")

    lines.append(f"**Time / 时间**  {when}")
    return '\n'.join(lines)


def fill_feishu_enabled(config: dict) -> bool:
    dual = config.get('dual_strategy') or {}
    return bool(dual.get('fill_feishu_enabled', True))


def unified_fill_feishu(config: dict) -> bool:
    dual = config.get('dual_strategy') or {}
    return bool(dual.get('unified_fill_feishu', True))


def notify_fill_trade(
    conn,
    trade: dict,
    row: Dict[str, str],
    config: dict,
    logger=None,
) -> bool:
    if not fill_feishu_enabled(config):
        return False
    msg = format_fill_feishu_message(row, trade)
    try:
        from auto_feishu import send_feishu_message
        ok = send_feishu_message(msg, config=config)
        if logger and ok:
            logger.debug(
                f'[FillFeishu] sent {row.get("instrument_code")} {row.get("fill_side")}'
            )
        return ok
    except Exception as e:
        if logger:
            logger.debug(f'[FillFeishu] send failed: {e}')
        return False


def notify_fill_trade_async(
    conn,
    trade: dict,
    row: Dict[str, str],
    config: dict,
    logger=None,
) -> None:
    """Send in background so CTP SPI callback is not blocked."""
    threading.Thread(
        target=notify_fill_trade,
        args=(conn, trade, row, config, logger),
        daemon=True,
        name='FillFeishuNotify',
    ).start()


def install_unified_trade_feishu(config: dict = None) -> None:
    """
    Route all fill Feishu alerts through fill_ledger + trade_feishu_notify.
    Suppresses legacy spread A/B leg messages to avoid duplicates.
    """
    import auto_feishu

    def _suppress(*_a, **_k) -> bool:
        return False

    auto_feishu.notify_order_filled = _suppress
    auto_feishu.FeishuNotifier.notify_order_filled = _suppress

    if config and logger_is_debug(config):
        import logging
        logging.getLogger(__name__).debug(
            'Unified fill Feishu installed (legacy notify_order_filled suppressed)'
        )


def logger_is_debug(config: dict) -> bool:
    return str(config.get('log_level', '')).upper() == 'DEBUG'
