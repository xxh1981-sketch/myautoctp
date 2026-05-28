#!/usr/bin/env python3
"""运行不依赖 autotrade / autostraggle 的 unit 测试（与 CI pytest-unit 一致）。"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# 无 AUTOTRADE_ROOT 时可收集；conftest 会注入 autotrade_stubs + pairtrade stub。
UNIT_TESTS = [
    'tests/test_atomic_io.py',
    'tests/test_check_sensitive_files.py',
    'tests/test_data_path_guard.py',
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
    'tests/test_startup_ack.py',
    'tests/test_startup_ack_fingerprint.py',
    'tests/test_startup_ack_derive.py',
    'tests/test_account_decomposition.py',
    'tests/test_external_ack_reconcile.py',
    'tests/test_external_ack_persist.py',
    'tests/test_invalidate_startup_ack_script.py',
    'tests/test_spread_ledger_execution.py',
    'tests/test_p4_banner_and_warning.py',
    'tests/test_merged_main_startup.py',
    'tests/test_merged_main_loop_limits.py',
    'tests/test_merged_main_loop.py',
    'tests/test_spread_daily_limit.py',
    'tests/test_order_whitelist_guard.py',
    'tests/test_trade_feishu_notify.py',
    'tests/test_wire_idempotent.py',
    'tests/test_spread_fill_sync.py',
    'tests/test_strangle_fill_sync.py',
    'tests/test_strangle_close_only_holdback.py',
    'tests/test_strangle_unmatched_watchdog.py',
    'tests/test_strangle_reconcile_dual.py',
    'tests/test_spread_ledger.py',
    'tests/test_strategy_order_ref.py',
]


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    run_env = os.environ.copy()
    run_env.setdefault('AUTOCTP_ALLOW_MISSING_DEPS', '1')
    cmd = [sys.executable, '-m', 'pytest', *UNIT_TESTS, '-q', '--tb=short', *sys.argv[1:]]
    return subprocess.call(cmd, cwd=root, env=run_env)


if __name__ == '__main__':
    raise SystemExit(main())
