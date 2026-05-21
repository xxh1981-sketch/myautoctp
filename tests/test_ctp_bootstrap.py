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
        # 恢复默认 bootstrap（允许 CI 缺 autotrade）
        os.environ['AUTOCTP_ALLOW_MISSING_DEPS'] = '1'
        try:
            _reimport_bootstrap()
        except RuntimeError:
            pass

    def test_strict_mode_raises_when_dir_missing(self):
        os.environ.pop('AUTOCTP_ALLOW_MISSING_DEPS', None)
        # 必须在 import 前 patch：ctp_bootstrap 模块加载时会立刻执行 setup_paths()
        with patch('ctp_bootstrap.os.path.isdir', return_value=False):
            with self.assertRaises(RuntimeError):
                _reimport_bootstrap()

    def test_allow_missing_returns_paths_without_raise(self):
        os.environ['AUTOCTP_ALLOW_MISSING_DEPS'] = '1'
        with patch('ctp_bootstrap.os.path.isdir', return_value=False):
            boot = _reimport_bootstrap()
            rt, rg = boot.setup_paths({})
        self.assertTrue(isinstance(rt, str))
        self.assertTrue(isinstance(rg, str))


if __name__ == '__main__':
    unittest.main()
