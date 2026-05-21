#!/usr/bin/env python3
"""运行不依赖 autotrade / autostraggle 的 unit 测试（与 CI pytest-unit 一致）。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

UNIT_TESTS = [
    'tests/test_atomic_io.py',
    'tests/test_merged_config.py',
    'tests/test_process_guard.py',
    'tests/test_trade_journal.py',
    'tests/test_runtime_risk_alerts.py',
    'tests/test_spread_claims_guard.py',
    'tests/test_strangle_positions_csv.py',
    'tests/test_ctp_bootstrap.py',
    'tests/test_ctp_heartbeat_guard.py',
    'tests/test_health_check_patch.py',
    'tests/test_ctp_recovery_patch.py',
    'tests/test_merged_tradeinfo.py',
    'tests/test_env_utils.py',
    'tests/test_merged_vix_cache.py',
    'tests/test_merged_strategy_logger.py',
    'tests/test_margin_check_unit.py',
    'tests/test_spread_position_adjust_unit.py',
    'tests/test_spread_derive_unit.py',
    'tests/test_spread_reconcile_unit.py',
    'tests/test_fill_ledger_unit.py',
]


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    cmd = [sys.executable, '-m', 'pytest', *UNIT_TESTS, '-q', '--tb=short', *sys.argv[1:]]
    return subprocess.call(cmd, cwd=root)


if __name__ == '__main__':
    raise SystemExit(main())
