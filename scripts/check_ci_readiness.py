#!/usr/bin/env python3
"""检查全量测试前置条件（不访问 GitHub API）。"""

from __future__ import annotations

import argparse
import importlib.util
import os
import re
import sys
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _load_unit_tests() -> list[str]:
    script = _repo_root() / 'scripts' / 'run_unit_tests.py'
    spec = importlib.util.spec_from_file_location('run_unit_tests', script)
    if spec is None or spec.loader is None:
        raise RuntimeError(f'无法加载 {script}')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return list(mod.UNIT_TESTS)


def check_unit_test_manifest() -> bool:
    """Verify UNIT_TESTS entries exist and doc counts match."""
    root = _repo_root()
    unit_tests = _load_unit_tests()
    ok = True
    missing = [p for p in unit_tests if not (root / p).is_file()]
    if missing:
        ok = False
        print('run_unit_tests.py 引用了不存在的测试文件：')
        for p in missing:
            print(f'  - {p}')

    count = len(unit_tests)
    docs_to_check = [
        root / 'CONTRIBUTING.md',
        root / 'docs' / 'CI.md',
    ]
    pattern = re.compile(
        r'run_unit_tests\.py[^）\n]*（(\d+)\s*个文件'
        r'|现含\s*\*\*(\d+)\*\*\s*个测试文件',
    )
    for doc in docs_to_check:
        if not doc.is_file():
            continue
        text = doc.read_text(encoding='utf-8')
        for match in pattern.finditer(text):
            doc_count = int(match.group(1) or match.group(2))
            if doc_count != count:
                ok = False
                print(
                    f'{doc.relative_to(root)} 写 {doc_count} 个文件，'
                    f'run_unit_tests.py 实际 {count} 个'
                )

    if ok:
        print(f'unit manifest OK: {count} 个文件')
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--strict-ci',
        action='store_true',
        help='同时要求 AUTOTRADE_REPO_URL / AUTOSTRAGGLE_REPO_URL 环境变量（模拟 GitHub secrets）',
    )
    parser.add_argument(
        '--check-unit-manifest',
        action='store_true',
        help='校验 run_unit_tests.py 清单与 CONTRIBUTING/docs/CI.md 文件数一致',
    )
    args = parser.parse_args()

    ok = True
    if args.check_unit_manifest or not args.strict_ci:
        ok = check_unit_test_manifest() and ok

    autotrade = os.environ.get('AUTOTRADE_ROOT', r'D:\autotrade')
    autostraggle = os.environ.get('AUTOSTRAGGLE_ROOT', r'D:\autostraggle')

    if args.strict_ci:
        if not os.path.isdir(autotrade):
            print(f'缺少 autotrade: {autotrade}（可设 AUTOTRADE_ROOT）')
            ok = False
        if not os.path.isdir(autostraggle):
            print(f'缺少 autostraggle: {autostraggle}（可设 AUTOSTRAGGLE_ROOT）')
            ok = False
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
    elif args.check_unit_manifest:
        print(
            '提示: pytest-full 需 AUTOTRADE_REPO_URL；'
            '私有库另需 DEPENDENCY_REPO_PAT（见 docs/CI.md）'
        )

    if ok:
        print('检查通过。')
        return 0
    return 1


if __name__ == '__main__':
    sys.exit(main())
