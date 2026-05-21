"""pytest 公共配置：自动区分 unit / integration 用例。"""

from __future__ import annotations

import importlib.util
import os
import sys

import pytest

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
