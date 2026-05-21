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
        if not os.environ.get('AUTOTRADE_REPO_URL', '').strip():
            print('缺少 GitHub secret 对应变量: AUTOTRADE_REPO_URL')
            ok = False
        url = os.environ.get('AUTOTRADE_REPO_URL', '').strip()
        if url and (
            url.startswith('https://github.com/')
            and '@' not in url
            and not os.environ.get('DEPENDENCY_REPO_PAT', '').strip()
        ):
            print(
                'AUTOTRADE_REPO_URL 为 GitHub HTTPS 且无嵌入 token；'
                '私有库还需 DEPENDENCY_REPO_PAT（见 docs/CI.md）'
            )
            ok = False
        if not os.environ.get('AUTOSTRAGGLE_REPO_URL', '').strip():
            print('提示: 未设 AUTOSTRAGGLE_REPO_URL，CI 将只跑 autotrade integration（见 docs/CI.md）')
    else:
        print(
            '提示: pytest-full 至少需要 AUTOTRADE_REPO_URL；'
            '私有库另需 DEPENDENCY_REPO_PAT；autostraggle 可选（见 docs/CI.md）'
        )

    if ok:
        print('检查通过。')
        return 0
    return 1


if __name__ == '__main__':
    sys.exit(main())
