"""pytest 公共配置：自动区分 unit / integration 用例。"""

from __future__ import annotations

import importlib.util
import os
import sys
import types

import pytest

from openctp_stubs import ensure_openctp_stub

ensure_openctp_stub()

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def _use_autotrade_stubs() -> bool:
    if os.environ.get('AUTOCTP_ALLOW_MISSING_DEPS', '').strip() == '1':
        root = os.environ.get('AUTOTRADE_ROOT', '').strip()
        return not (root and os.path.isdir(root))
    root = os.environ.get('AUTOTRADE_ROOT', '').strip()
    if root and os.path.isdir(root):
        return False
    return not os.path.isdir(r'D:\autotrade')


def _ensure_pairtrade_constants_stub() -> None:
    if 'pairtrade.constants' in sys.modules:
        return
    _pt = types.ModuleType('pairtrade')
    _pt_const = types.ModuleType('pairtrade.constants')
    _pt_const.DIRECTION_BUY = '0'
    _pt_const.DIRECTION_SELL = '1'
    _pt_const.OFFSET_OPEN = '0'
    _pt_const.OFFSET_CLOSE = '1'
    _pt_const.OFFSET_CLOSE_TODAY = '3'
    _pt_const.OFFSET_CLOSE_YESTERDAY = '4'
    _pt_config = types.ModuleType('pairtrade.config')
    _pt_config.adjust_price = lambda price, tick: float(price)
    _pt.constants = _pt_const
    _pt.config = _pt_config
    sys.modules['pairtrade'] = _pt
    sys.modules['pairtrade.constants'] = _pt_const
    sys.modules['pairtrade.config'] = _pt_config


import autotrade_stubs

if _use_autotrade_stubs():
    # Only for pytest-unit (no autotrade checkout): stub before @patch resolves.
    autotrade_stubs.ensure_auto_feishu_stub()
    autotrade_stubs.ensure_autotrade_stubs(autotrade_stubs.ALL_STUB_MODULES)
    autotrade_stubs.ensure_autostraggle_stubs()
    _ensure_pairtrade_constants_stub()
else:
    # pytest-full: inject AUTOTRADE_ROOT / AUTOSTRAGGLE_ROOT into sys.path
    # before any test module imports spread_ledger (auto_connection) etc.
    import ctp_bootstrap  # noqa: F401
    autotrade_stubs.ensure_auto_feishu_stub()


def _bootstrap_test_deps() -> None:
    """Idempotent: ensure autotrade paths or stubs before test module import."""
    if _use_autotrade_stubs():
        autotrade_stubs.ensure_auto_feishu_stub()
        autotrade_stubs.ensure_autotrade_stubs(autotrade_stubs.ALL_STUB_MODULES)
        autotrade_stubs.ensure_autostraggle_stubs()
        _ensure_pairtrade_constants_stub()
    else:
        import ctp_bootstrap  # noqa: F401
        autotrade_stubs.ensure_auto_feishu_stub()


def pytest_configure(config) -> None:
    _bootstrap_test_deps()


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
    if os.environ.get('AUTOCTP_ALLOW_MISSING_DEPS', '').strip() == '1':
        root = os.environ.get('AUTOTRADE_ROOT', '').strip()
        return bool(root and os.path.isdir(root))
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
    # pytest-full (-m "not unit") 仍会 import 测试模块；unit 已在 pytest-unit 覆盖，跳过收集。
    if (
        _autotrade_root_available()
        and os.environ.get('AUTOCTP_ALLOW_MISSING_DEPS', '').strip() != '1'
        and name in _unit_test_basenames()
    ):
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
