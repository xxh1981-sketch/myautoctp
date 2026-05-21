"""ctp_bootstrap soft-fail mode tests."""

import importlib
import os
import sys
import unittest
from unittest.mock import patch


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _reimport_bootstrap():
    if REPO_ROOT not in sys.path:
        sys.path.insert(0, REPO_ROOT)
    sys.modules.pop('ctp_bootstrap', None)
    return importlib.import_module('ctp_bootstrap')


class TestCtpBootstrapSoftFail(unittest.TestCase):

    def setUp(self):
        self._saved_env = {}
        for k in ('AUTOCTP_ALLOW_MISSING_DEPS', 'AUTOTRADE_ROOT', 'AUTOSTRAGGLE_ROOT'):
            self._saved_env[k] = os.environ.get(k)

    def tearDown(self):
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_strict_mode_raises_when_dir_missing(self):
        os.environ.pop('AUTOCTP_ALLOW_MISSING_DEPS', None)
        boot = _reimport_bootstrap()
        # patch os.path.isdir 让 ctp_bootstrap 看到 "什么目录都不存在"，
        # 模拟 CI 环境（无 D:\autotrade fallback）。
        with patch('ctp_bootstrap.os.path.isdir', return_value=False):
            with self.assertRaises(RuntimeError):
                boot.setup_paths({})

    def test_allow_missing_returns_paths_without_raise(self):
        os.environ['AUTOCTP_ALLOW_MISSING_DEPS'] = '1'
        boot = _reimport_bootstrap()
        with patch('ctp_bootstrap.os.path.isdir', return_value=False):
            rt, rg = boot.setup_paths({})
        self.assertTrue(isinstance(rt, str))
        self.assertTrue(isinstance(rg, str))


if __name__ == '__main__':
    unittest.main()
