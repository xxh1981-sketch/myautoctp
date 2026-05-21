#!/usr/bin/env python3
"""开盘前离线检查：CSV / 账本格式与配置项（不连 CTP，不做完整对账）。

用法:
  python scripts/preview_startup_data.py
  python scripts/preview_startup_data.py -c merged_config.yaml
"""

from __future__ import annotations

import argparse
import json
import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)


def _load_config(path: str) -> dict:
    from merged_config import load_merged_config

    return load_merged_config(path)


def _check_csv(path: str, label: str, errors: list, warnings: list) -> None:
    if not os.path.isfile(path):
        warnings.append(f'{label}: 文件不存在 {path}')
        return
    try:
        import csv

        with open(path, encoding='utf-8-sig', newline='') as f:
            rows = list(csv.DictReader(f))
    except Exception as e:
        errors.append(f'{label}: 读取失败 {path}: {e}')
        return
    if not rows:
        warnings.append(f'{label}: 空表（{path}）')
        return
    print(f'  {label}: {len(rows)} 行 @ {path}')


def _check_ledger(path: str, label: str, warnings: list) -> None:
    if not os.path.isfile(path):
        warnings.append(f'{label}: 不存在 {path}')
        return
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        warnings.append(f'{label}: JSON 无效 {path}: {e}')
        return
    daily = data.get('daily_buy_amount') or {}
    unmatched = data.get('unmatched_legs') or []
    print(
        f'  {label}: daily_buy_amount 键 {len(daily)}, '
        f'unmatched_legs {len(unmatched)} @ {path}'
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '-c', '--config', default=os.path.join(_REPO, 'merged_config.yaml'),
        help='merged_config 路径',
    )
    args = parser.parse_args()

    errors: list[str] = []
    warnings: list[str] = []

    if not os.path.isfile(args.config):
        errors.append(f'配置文件不存在: {args.config}')
        for msg in errors:
            print(f'ERROR: {msg}')
        return 1

    cfg = _load_config(args.config)
    merged_errors, merged_warnings = [], []
    try:
        from merged_config import _validate_merged_config

        merged_errors, merged_warnings = _validate_merged_config(cfg)
    except Exception as e:
        warnings.append(f'配置校验跳过: {e}')

    dual = cfg.get('dual_strategy') or {}
    str_cfg = cfg.get('strangle') or {}

    print('== 配置摘要 ==')
    print(f"  global_margin_limit={cfg.get('global_margin_limit', '(默认)')}")
    print(f"  daily_trade_limit={cfg.get('daily_trade_limit', '(继承 autotrade)')}")
    print(f"  strangle.daily_buy_limit_yuan={str_cfg.get('daily_buy_limit_yuan', 300000)}")
    print(f"  require_startup_ack={dual.get('require_startup_ack', True)}")
    print(f"  startup_ack_file={dual.get('startup_ack_file', 'data/position_startup_ack.txt')}")

    print('== 持仓 CSV ==')
    _check_csv(
        dual.get('spread_positions_csv') or 'data/spread_positions.csv',
        'spread_positions',
        errors,
        warnings,
    )
    _check_csv('data/strangle_positions.csv', 'strangle_positions', errors, warnings)

    print('== 宽跨账本 ==')
    ledger_path = str_cfg.get('ledger_path') or 'data/ledger_strangle.json'
    _check_ledger(ledger_path, 'ledger_strangle', warnings)

    ack_file = dual.get('startup_ack_file') or 'data/position_startup_ack.txt'
    if dual.get('startup_ack_persist', True) and os.path.isfile(ack_file):
        print(f'  startup_ack: 存在 {ack_file}')
    elif dual.get('require_startup_ack', True):
        warnings.append(f'启动确认文件不存在: {ack_file}（首次或需重确认）')

    for w in merged_warnings + warnings:
        print(f'WARN: {w}')
    for e in merged_errors + errors:
        print(f'ERROR: {e}')

    print()
    print('说明: 完整 CTP 对账预览需启动 merged_main 或连接柜台；本脚本仅做离线文件检查。')

    return 1 if (merged_errors or errors) else 0


if __name__ == '__main__':
    raise SystemExit(main())
