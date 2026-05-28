#!/usr/bin/env python3
"""失效启动确认，并可选清理换账户相关运行时文件。"""

from __future__ import annotations

import argparse
import glob
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from merged_config import load_merged_config
from startup_ack_fingerprint import invalidate_startup_ack_files


def _project_root() -> str:
    return ROOT


def _abs_path(path: str) -> str:
    p = str(path or '').strip()
    if not p:
        return p
    if os.path.isabs(p):
        return p
    return os.path.join(_project_root(), p)


def _delete_file(path: str, removed: list[str]) -> None:
    if path and os.path.isfile(path):
        try:
            os.remove(path)
            removed.append(path)
        except OSError:
            pass


def _delete_journal_family(journal_base: str, removed: list[str]) -> None:
    base = _abs_path(journal_base)
    if not base:
        return
    _delete_file(base, removed)
    stem, ext = os.path.splitext(base)
    pattern = f'{stem}-*{ext}'
    for path in glob.glob(pattern):
        _delete_file(path, removed)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '-c',
        '--config',
        default=None,
        help='merged_config 路径（默认使用仓库根 merged_config.yaml）',
    )
    parser.add_argument(
        '--include-journals',
        action='store_true',
        help='额外删除 spread/strangle/fill 的 journal（含按日分片）',
    )
    parser.add_argument(
        '--include-fill-ledger',
        action='store_true',
        help='额外删除 fill_ledger.csv',
    )
    parser.add_argument(
        '--reset-strangle-runtime',
        action='store_true',
        help='将 ledger_strangle.json 运行时字段清零（保留 CSV 认领）',
    )
    parser.add_argument(
        '--account-switch',
        action='store_true',
        help='换账户模式：等价于 --include-journals --include-fill-ledger --reset-strangle-runtime',
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        config = load_merged_config(args.config) if args.config else load_merged_config()
    except Exception as e:
        print(f'加载配置失败: {e}', file=sys.stderr)
        return 1

    if args.account_switch:
        args.include_journals = True
        args.include_fill_ledger = True
        args.reset_strangle_runtime = True

    removed = invalidate_startup_ack_files(config)
    dual = config.get('dual_strategy') or {}

    if args.include_journals:
        _delete_journal_family(
            dual.get('spread_trade_journal', 'data/spread_trade_journal.jsonl'),
            removed,
        )
        _delete_journal_family(
            dual.get('strangle_trade_journal', 'data/strangle_trade_journal.jsonl'),
            removed,
        )
        _delete_journal_family(
            dual.get('fill_ledger_journal', 'data/fill_ledger_journal.jsonl'),
            removed,
        )

    if args.include_fill_ledger:
        _delete_file(
            _abs_path(dual.get('fill_ledger_csv', 'data/fill_ledger.csv')),
            removed,
        )

    runtime_reset = False
    if args.reset_strangle_runtime:
        try:
            from import_strangle_positions import import_csv_to_ledger, positions_csv_path

            str_cfg = config.get('strangle') or {}
            csv_path = _abs_path(positions_csv_path(config))
            ledger_path = _abs_path(str_cfg.get('ledger_path') or 'data/ledger_strangle.json')
            import_csv_to_ledger(csv_path, ledger_path, preserve_runtime=False)
            runtime_reset = True
        except Exception as e:
            print(f'WARN: 重置宽跨 runtime 失败: {e}', file=sys.stderr)

    if not removed:
        print('未发现可删除文件（可能已删或路径不同）。')
        if runtime_reset:
            print('已重置 ledger_strangle runtime 字段（保留 CSV 认领）。')
        return 0
    print('已删除:')
    for p in removed:
        print(f'  {p}')
    if runtime_reset:
        print('已重置 ledger_strangle runtime 字段（保留 CSV 认领）。')
    print('请人工冷启动 merged_main.py 重新核对持仓。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
