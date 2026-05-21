"""Spread daily open-count limit resolution for the main loop."""


def resolve_spread_daily_limit(
    fc,
    spread_daily_limit: int,
    spread_open_ok: bool,
    log_warning=None,
) -> tuple[int, bool]:
    """Resolve spread filled count and whether new opens remain allowed.

    When ``fc is None`` (both spread-specific and account-wide queries failed),
    conservatively block new opens but do **not** cancel pending orders — callers
    should still scan for closes.

    Returns:
        ``(spread_filled, spread_open_ok)`` after applying daily-limit rules.
    """
    if fc is None:
        if log_warning is not None:
            log_warning(
                '日限笔数查询失败，本轮保守禁新开（保留在途单），仍扫描平仓'
            )
        return spread_daily_limit, False

    spread_filled = fc
    if fc >= spread_daily_limit:
        return spread_filled, False
    return spread_filled, spread_open_ok
