"""Block accidental writes to repo data/ during pytest (持仓/账本/journal 隔离)."""

from __future__ import annotations

import os
import sys

_PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_DATA_DIR = os.path.join(_PROJECT_DIR, 'data')


def repo_data_dir() -> str:
    return _REPO_DATA_DIR


def is_repo_data_path(path: str) -> bool:
    if not path:
        return False
    abs_path = os.path.normcase(os.path.abspath(path))
    data_dir = os.path.normcase(os.path.abspath(_REPO_DATA_DIR))
    return abs_path == data_dir or abs_path.startswith(data_dir + os.sep)


def under_pytest() -> bool:
    if os.environ.get('PYTEST_CURRENT_TEST'):
        return True
    # python -m pytest 子进程 / 部分 IDE 跑法
    if any('pytest' in (os.path.basename(arg or '') or '') for arg in sys.argv):
        return True
    return 'pytest' in sys.modules and bool(os.environ.get('PYTEST_VERSION'))


def guard_repo_data_write(path: str) -> None:
    """Raise if a test tries to write under repo data/."""
    if not under_pytest():
        return
    if is_repo_data_path(path):
        raise RuntimeError(
            f'测试禁止写入生产 data/ 路径: {path}。'
            '请用 tempfile，并在 config 中设置 spread_positions_csv / '
            'strangle_positions_csv / spread_trade_journal 等指向临时文件。'
        )
