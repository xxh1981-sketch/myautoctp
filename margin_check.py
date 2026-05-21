"""Global margin limit checks (startup + periodic main-loop).

Three-state result design
-------------------------
`check_margin_status` returns one of:

- ``'ok'``         — within ``global_margin_limit`` (or limit disabled).
- ``'over_limit'`` — definitively over the configured limit; safe to halt opens.
- ``'unknown'``    — could not determine (e.g. CTP position query failed every
  retry). Callers SHOULD preserve the previous halt state and avoid flipping
  a clean account into ``'保证金超限'`` purely because of a transient CTP
  read failure.

The legacy :func:`check_margin` wrapper is kept for backward compatibility so
older callers continue to receive ``True``/``False``. New code should call
:func:`check_margin_status` and react to ``'unknown'`` explicitly.
"""

from __future__ import annotations

from typing import Literal, Tuple

MarginStatus = Literal['ok', 'over_limit', 'unknown']


def check_margin_status(
    conn, config: dict, logger, context: str = '',
) -> Tuple[MarginStatus, str]:
    """Return ``(status, reason)`` describing the global margin state.

    ``reason`` is a short Chinese phrase suitable for surfacing in the
    runtime ``_margin_halt_reason`` field; it is empty when ``status`` is
    ``'ok'``.
    """
    limit = config.get('global_margin_limit', 0)
    if limit <= 0:
        runtime = getattr(conn, '_runtime_state', None)
        if runtime is not None and not runtime.get('_margin_disabled_warned'):
            runtime['_margin_disabled_warned'] = True
            logger.warning(
                '保证金风控: 已禁用 (global_margin_limit=0)，'
                '主循环不会因保证金超限自动暂停新开'
            )
        return 'ok', ''

    prefix = f'保证金检查{(" (" + context + ")") if context else ""}'
    retry = config.get('margin_retry_interval', 30)
    max_attempts = config.get('margin_check_max_attempts', 3)
    from auto_risk import sum_positions_margin_for_limit

    for attempt in range(max_attempts):
        pos = conn.query_positions_sync(timeout=10)
        if pos is None:
            logger.warning(f'{prefix}: 持仓查询失败 ({attempt + 1}/{max_attempts})')
            if attempt + 1 < max_attempts:
                import time
                time.sleep(retry)
            continue
        total, _ = sum_positions_margin_for_limit(conn, pos, config)
        if total > limit:
            reason = f'保证金超限 {total:.2f} > {limit:.2f}'
            logger.error(f'{prefix}: 超限 {total:.2f} > {limit:.2f}')
            return 'over_limit', reason
        return 'ok', ''

    logger.error(
        f'{prefix}: 持仓查询多次失败 ({max_attempts} 次)，无法判定保证金，'
        '保留上一轮风控状态'
    )
    return 'unknown', '持仓查询失败，无法判定保证金'


def check_margin(conn, config: dict, logger, context: str = '') -> bool:
    """Legacy True/False wrapper.

    Returns ``True`` when status is ``'ok'`` and ``False`` only when status is
    ``'over_limit'``. For backward compatibility with callers that expected the
    old "fail-safe = treat unknown as over_limit" behavior, ``'unknown'`` also
    returns ``False`` here — new callers should prefer
    :func:`check_margin_status` and special-case ``'unknown'`` to preserve the
    previous halt state.
    """
    status, _ = check_margin_status(conn, config, logger, context=context)
    return status == 'ok'
