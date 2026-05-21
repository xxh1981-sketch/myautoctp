#!/usr/bin/env python3
"""检查全量测试前置条件（不访问 GitHub API）。"""

from __future__ import annotations

import argparse
import os
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--strict-ci',
        action='store_true',
        help='同时要求 AUTOTRADE_REPO_URL / AUTOSTRAGGLE_REPO_URL 环境变量（模拟 GitHub secrets）',
    )
    args = parser.parse_args()

    ok = True
    autotrade = os.environ.get('AUTOTRADE_ROOT', r'D:\autotrade')
    autostraggle = os.environ.get('AUTOSTRAGGLE_ROOT', r'D:\autostraggle')

    if not os.path.isdir(autotrade):
        print(f'缺少 autotrade: {autotrade}（可设 AUTOTRADE_ROOT）')
        ok = False
    if not os.path.isdir(autostraggle):
        print(f'缺少 autostraggle: {autostraggle}（可设 AUTOSTRAGGLE_ROOT）')
        ok = False

    if args.strict_ci:
        for name in ('AUTOTRADE_REPO_URL', 'AUTOSTRAGGLE_REPO_URL'):
            if not os.environ.get(name, '').strip():
                print(f'缺少 GitHub secret 对应变量: {name}')
                ok = False
    else:
        print('提示: GitHub Actions 全量 CI 需配置 AUTOTRADE_REPO_URL / AUTOSTRAGGLE_REPO_URL（见 docs/CI.md）')

    if ok:
        print('检查通过。')
        return 0
    return 1


if __name__ == '__main__':
    sys.exit(main())
