#!/usr/bin/env python3
"""CI 护栏：阻止误提交运行时数据与本地配置（规则与 .gitignore 对齐）。"""

from __future__ import annotations

import subprocess
import sys

BLOCKED_EXACT = {
    "merged_config.yaml",
    "merged_config.local.yaml",
    ".env",
}

# 与 .gitignore「本地专用文档」一致
BLOCKED_DOCS_EXACT = {
    "docs/GUIDE.md",
    "docs/LOCAL完整说明.md",
    "docs/LOCAL-CL工作清单.md",
}


def _is_allowed_repo_subdir_path(path: str, prefix: str, keep_path: str) -> bool:
    """允许 <prefix>.gitkeep 与文件名含 example 的模板。"""
    if path == keep_path:
        return True
    if not path.startswith(prefix):
        return False
    name = path.rsplit("/", 1)[-1].lower()
    return "example" in name


def _is_blocked_path(path: str) -> bool:
    if path in BLOCKED_EXACT or path in BLOCKED_DOCS_EXACT:
        return True
    if path.startswith(".cursor/"):
        return True
    if path.startswith("data/") and not _is_allowed_repo_subdir_path(path, "data/", "data/.gitkeep"):
        return True
    if path.startswith("tradeinfo/") and not _is_allowed_repo_subdir_path(
        path, "tradeinfo/", "tradeinfo/.gitkeep"
    ):
        return True
    if path.startswith("futuretrade/"):
        return True
    return False


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

    violations = [p for p in (ln.strip().replace("\\", "/") for ln in out.splitlines()) if p and _is_blocked_path(p)]

    if violations:
        print("以下敏感/运行时文件不应被 git 跟踪：")
        for p in violations:
            print(f"  - {p}")
        print("请 git rm --cached 并从提交中移除。")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
