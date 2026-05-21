"""pytest 公共配置：自动区分 unit / integration 用例。"""

from __future__ import annotations

import importlib.util
import os
import sys

import pytest

from openctp_stubs import ensure_openctp_stub

ensure_openctp_stub()

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def _unit_test_basenames() -> set[str]:
    script = os.path.join(_REPO_ROOT, 'scripts', 'run_unit_tests.py')
    spec = importlib.util.spec_from_file_location('run_unit_tests', script)
    if spec is None or spec.loader is None:
        return set()
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return {os.path.basename(p) for p in mod.UNIT_TESTS}


def _autostraggle_test_basenames() -> frozenset[str]:
    return frozenset({
        'test_strangle_ledger_atomic.py',
        'test_strangle_rebalance_close_only.py',
        'test_spread_reconcile.py',
        'test_merged_main_loop.py',
        'test_merged_main_loop_limits.py',
    })


def _autotrade_root_available() -> bool:
    root = os.environ.get('AUTOTRADE_ROOT', '').strip()
    if root and os.path.isdir(root):
        return True
    return os.path.isdir(r'D:\autotrade')


def pytest_ignore_collect(collection_path, config):
    name = collection_path.name
    if not (name.startswith('test_') and name.endswith('.py')):
        return False
    if os.environ.get('CI_AUTOTRADE_ONLY', '').strip() == '1':
        if name in _autostraggle_test_basenames():
            return True
    if not _autotrade_root_available() and name not in _unit_test_basenames():
        return True
    return False


def pytest_collection_modifyitems(config, items) -> None:
    unit_files = _unit_test_basenames()
    autostraggle_files = _autostraggle_test_basenames()
    for item in items:
        if item.path.name in unit_files:
            item.add_marker(pytest.mark.unit)
        else:
            item.add_marker(pytest.mark.integration)
        if item.path.name in autostraggle_files:
            item.add_marker(pytest.mark.autostraggle)
