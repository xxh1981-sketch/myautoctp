#!/usr/bin/env python3
"""只读运维状态摘要：读取心跳文件 + 配置，输出运行健康面板。

  .\\.venv\\Scripts\\python scripts/ops_status.py
  .\\.venv\\Scripts\\python scripts/ops_status.py --json
"""

from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import ctp_bootstrap  # noqa: F401


def _abs_path(path: str) -> str:
    p = str(path or '').strip()
    if not p:
        return p
    if os.path.isabs(p):
        return p
    return os.path.join(ROOT, p)


def _read_heartbeat(config: dict) -> str:
    hb = str(config.get('heartbeat_file') or 'data/main_loop_heartbeat.txt')
    path = _abs_path(hb)
    if not os.path.isfile(path):
        return f'心跳文件不存在: {path}'
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return f.read().strip()
    except OSError as e:
        return f'读取心跳失败: {e}'


def run(*, as_json: bool = False) -> int:
    from merged_config import load_merged_config
    from maintenance_mode import is_maintenance_mode
    from position_csv_integrity import validate_position_csvs
    from runtime_health_summary import (
        build_runtime_health_summary,
        format_runtime_health_text,
    )
    from unattended_heartbeat import read_restart_reason_summary

    config = load_merged_config()
    summary = build_runtime_health_summary(None, config, ledger=None)
    summary['maintenance_mode'] = is_maintenance_mode(config)
    summary['restart_reason'] = read_restart_reason_summary(config)
    summary['heartbeat_file_content'] = _read_heartbeat(config)
    csv_errors = validate_position_csvs(config)
    summary['position_csv_errors'] = csv_errors

    if as_json:
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
        return 0

    text = format_runtime_health_text(
        summary,
        restart_reason=summary.get('restart_reason') or '',
    )
    print('=== AutoCTP 运维状态（只读） ===')
    print(text)
    if csv_errors:
        print('')
        print('持仓 CSV 校验:')
        for err in csv_errors:
            print(f'  - {err}')
    print('')
    print('--- 心跳文件 ---')
    print(summary['heartbeat_file_content'])
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description='AutoCTP 只读运维状态')
    parser.add_argument('--json', action='store_true', help='JSON 输出')
    args = parser.parse_args()
    return run(as_json=args.json)


if __name__ == '__main__':
    raise SystemExit(main())
