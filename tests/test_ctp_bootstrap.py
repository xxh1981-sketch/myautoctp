"""ctp_bootstrap soft-fail mode tests."""

import os
import unittest
from unittest.mock import patch

import ctp_bootstrap as boot


class TestCtpBootstrapSoftFail(unittest.TestCase):
    """直接调用 setup_paths，避免 reimport 触发模块级副作用。"""

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
        with patch.object(boot.os.path, 'isdir', return_value=False):
            with self.assertRaises(RuntimeError):
                boot.setup_paths({})

    def test_allow_missing_returns_paths_without_raise(self):
        os.environ['AUTOCTP_ALLOW_MISSING_DEPS'] = '1'
        with patch.object(boot.os.path, 'isdir', return_value=False):
            rt, rg = boot.setup_paths({})
        self.assertTrue(isinstance(rt, str))
        self.assertTrue(isinstance(rg, str))


if __name__ == '__main__':
    unittest.main()
