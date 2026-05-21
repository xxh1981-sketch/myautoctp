#!/usr/bin/env python3
"""CI 护栏：阻止误提交运行时数据与本地配置。"""

from __future__ import annotations

import subprocess
import sys

BLOCKED_PREFIXES = (
    "data/",
    "data\\",
)
BLOCKED_EXACT = {
    "merged_config.yaml",
    "merged_config.local.yaml",
    ".env",
}


def main() -> int:
    try:
        out = subprocess.check_output(
            ["git", "ls-files"],
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print(f"check_sensitive_files: 无法读取 git 索引: {e}")
        return 0

    violations = []
    for line in out.splitlines():
        path = line.strip().replace("\\", "/")
        if not path:
            continue
        if path in BLOCKED_EXACT or any(path.startswith(p) for p in BLOCKED_PREFIXES):
            violations.append(path)

    if violations:
        print("以下敏感/运行时文件不应被 git 跟踪：")
        for p in violations:
            print(f"  - {p}")
        print("请 git rm --cached 并从提交中移除。")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
