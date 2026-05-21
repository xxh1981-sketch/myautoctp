"""Stub openctp_ctp for CI / local pytest without CTP runtime."""

from __future__ import annotations

import os
import sys
import types
from unittest.mock import MagicMock


class _FakeTraderSpi:
    def __init__(self):
        pass


class _FakeMdSpi:
    def __init__(self):
        pass


class _AutoMockModule(types.ModuleType):
    """Return MagicMock for undefined CTP field / API symbols."""

    def __getattr__(self, name):
        if name.startswith('_'):
            raise AttributeError(name)
        return MagicMock


def _install_openctp_stub() -> None:
    tdapi = _AutoMockModule('openctp_ctp.tdapi')
    mdapi = _AutoMockModule('openctp_ctp.mdapi')
    tdapi.CThostFtdcTraderSpi = _FakeTraderSpi
    mdapi.CThostFtdcMdSpi = _FakeMdSpi

    pkg = _AutoMockModule('openctp_ctp')
    pkg.tdapi = tdapi
    pkg.mdapi = mdapi
    pkg.__autoctp_stub__ = True  # type: ignore[attr-defined]

    sys.modules['openctp_ctp'] = pkg
    sys.modules['openctp_ctp.tdapi'] = tdapi
    sys.modules['openctp_ctp.mdapi'] = mdapi


def should_stub_openctp() -> bool:
    if os.environ.get('AUTOCTP_NO_STUB_OPENCTP', '').strip().lower() in ('1', 'true', 'yes'):
        return False
    if os.environ.get('AUTOCTP_STUB_OPENCTP', '').strip().lower() in ('1', 'true', 'yes'):
        return True
    if os.environ.get('GITHUB_ACTIONS') == 'true':
        return True
    return False


def ensure_openctp_stub() -> None:
    """Register stub before autotrade imports openctp_ctp (real lib aborts on GHA)."""
    if not should_stub_openctp():
        return
    existing = sys.modules.get('openctp_ctp')
    if existing is not None and getattr(existing, '__autoctp_stub__', False):
        return
    _install_openctp_stub()
