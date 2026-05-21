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
        os.environ.pop('AUTOTRADE_ROOT', None)
        os.environ.pop('AUTOSTRAGGLE_ROOT', None)
        with patch.object(boot.os.path, 'isdir', return_value=False):
            rt, rg = boot.setup_paths({})
        self.assertEqual(rt, '')
        self.assertEqual(rg, '')


    def test_allow_missing_does_not_append_default_roots(self):
        """allow_missing 且未显式配置时，setup_paths 不应把内置 D:\\ 默认路径入 path。"""
        os.environ['AUTOCTP_ALLOW_MISSING_DEPS'] = '1'
        os.environ.pop('AUTOTRADE_ROOT', None)
        os.environ.pop('AUTOSTRAGGLE_ROOT', None)
        before = set(boot.sys.path)
        with patch.object(boot.os.path, 'isdir', return_value=True):
            rt, rg = boot.setup_paths({})
        after = set(boot.sys.path)
        self.assertEqual(rt, '')
        self.assertEqual(rg, '')
        for default in (r'D:\autotrade', r'D:\autostraggle'):
            abspath = boot.os.path.abspath(default)
            self.assertNotIn(
                abspath,
                after - before,
                f'allow_missing 时不应新加入默认路径 {abspath}',
            )


if __name__ == '__main__':
    unittest.main()
