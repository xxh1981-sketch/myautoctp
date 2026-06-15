"""持仓 CSV 完整性校验（spread / strangle positions）。

空表合法；损坏（半行、缺列、非法数值、重复冲突）→ ``_position_csv_halt_open``。
纯风控门闸：禁新开；对账 halt 的 close-only 路径不受影响。
"""

from __future__ import annotations

import os
import time
from typing import List, Tuple

DEFAULT_CHECK_INTERVAL_SEC = 3600.0


def _project_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def _abs_path(path: str) -> str:
    p = str(path or '').strip()
    if not p:
        return p
    if os.path.isabs(p):
        return p
    return os.path.join(_project_dir(), p)


def validate_spread_positions_csv(path: str) -> Tuple[bool, str]:
    """Return (ok, error). Missing file or empty file is OK."""
    path = _abs_path(path)
    if not os.path.isfile(path):
        return True, ''
    size = os.path.getsize(path)
    if size == 0:
        return True, ''
    try:
        from import_spread_positions import load_spread_positions_csv
        load_spread_positions_csv(path)
        return True, ''
    except Exception as e:
        return False, str(e)


def validate_strangle_positions_csv(path: str) -> Tuple[bool, str]:
    path = _abs_path(path)
    if not os.path.isfile(path):
        return True, ''
    size = os.path.getsize(path)
    if size == 0:
        return True, ''
    try:
        from import_strangle_positions import load_positions_csv
        load_positions_csv(path)
        return True, ''
    except Exception as e:
        return False, str(e)


def validate_position_csvs(config: dict) -> List[str]:
    """Return human-readable errors; empty list means all OK."""
    dual = config.get('dual_strategy') or {}
    errors: List[str] = []
    spread_path = dual.get('spread_positions_csv', 'data/spread_positions.csv')
    ok, err = validate_spread_positions_csv(spread_path)
    if not ok:
        errors.append(f'spread_positions: {err}')
    str_path = dual.get('strangle_positions_csv', 'data/strangle_positions.csv')
    ok, err = validate_strangle_positions_csv(str_path)
    if not ok:
        errors.append(f'strangle_positions: {err}')
    return errors


def apply_position_csv_integrity_halt(
    conn,
    config: dict,
    logger=None,
    *,
    context: str = '运行中',
) -> bool:
    """Validate CSVs and set/clear ``_position_csv_halt_open``. Returns halt active."""
    if not bool(config.get('position_csv_integrity_enabled', True)):
        runtime = getattr(conn, '_runtime_state', None) or {}
        runtime['_position_csv_halt_open'] = False
        runtime['_position_csv_halt_reason'] = ''
        return False

    runtime = getattr(conn, '_runtime_state', None)
    if runtime is None:
        return False

    errors = validate_position_csvs(config)
    if errors:
        reason = '; '.join(errors[:3])
        prev = bool(runtime.get('_position_csv_halt_open'))
        runtime['_position_csv_halt_open'] = True
        runtime['_position_csv_halt_reason'] = reason
        if not prev and logger:
            logger.error(
                '[CSV完整性] %s检测到持仓 CSV 损坏，已 position_csv_halt: %s',
                context,
                reason,
            )
        return True

    if runtime.get('_position_csv_halt_open') and logger:
        logger.info('[CSV完整性] 持仓 CSV 已恢复有效，解除 position_csv_halt')
    runtime['_position_csv_halt_open'] = False
    runtime['_position_csv_halt_reason'] = ''
    return False


def maybe_check_position_csv_integrity(conn, config: dict, logger=None) -> bool:
    """Periodic check; returns True if halt is active after check."""
    if not bool(config.get('position_csv_integrity_enabled', True)):
        return False
    runtime = getattr(conn, '_runtime_state', None)
    if runtime is None:
        return False
    interval = float(
        config.get('position_csv_check_interval_sec', DEFAULT_CHECK_INTERVAL_SEC)
        or DEFAULT_CHECK_INTERVAL_SEC,
    )
    now = time.time()
    last = float(runtime.get('_position_csv_last_check_at', 0.0) or 0.0)
    if interval > 0 and now - last < interval:
        return bool(runtime.get('_position_csv_halt_open'))
    runtime['_position_csv_last_check_at'] = now
    return apply_position_csv_integrity_halt(
        conn, config, logger, context='周期检查',
    )


def run_startup_position_csv_check(config: dict, logger=None) -> None:
    """Fail-fast optional startup validation before main loop."""
    if not bool(config.get('position_csv_integrity_enabled', True)):
        return
    errors = validate_position_csvs(config)
    if not errors:
        return
    msg = '持仓 CSV 完整性检查失败:\n' + '\n'.join(f'- {e}' for e in errors)
    if logger:
        logger.error('[CSV完整性] %s', msg.replace('\n', ' | '))
    if bool(config.get('fail_fast_on_position_csv_corrupt', False)):
        raise ValueError(msg)
    if logger:
        logger.warning(
            '[CSV完整性] fail_fast_on_position_csv_corrupt=false，'
            '将在主循环内设置 position_csv_halt',
        )
