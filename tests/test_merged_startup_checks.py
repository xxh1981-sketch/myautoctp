"""merged_startup_checks 兼容锁单元测试。"""

import os
import tempfile
import unittest
from unittest.mock import patch

import yaml

from merged_startup_checks import audit_repo_compat_lock


class _FakeLogger:
    def __init__(self):
        self.logs = []

    def info(self, msg, *args, **kwargs):
        self.logs.append(('info', msg % args if args else msg))

    def warning(self, msg, *args, **kwargs):
        self.logs.append(('warning', msg % args if args else msg))

    def error(self, msg, *args, **kwargs):
        self.logs.append(('error', msg % args if args else msg))


class TestCompatLockAudit(unittest.TestCase):
    def test_missing_lock_file_warn_only(self):
        logger = _FakeLogger()
        cfg = {
            'compat_lock_path': r'Z:\not-exist\compat_lock.yaml',
            'compat_lock_enforce': False,
        }
        self.assertTrue(audit_repo_compat_lock(cfg, logger))
        self.assertTrue(any(level == 'warning' for level, _ in logger.logs))

    def test_mismatch_with_enforce_blocks_startup(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = os.path.join(tmp, 'compat_lock.yaml')
            with open(lock_path, 'w', encoding='utf-8') as f:
                yaml.safe_dump(
                    {'expected_commits': {
                        'autoctp': 'aaa111',
                        'autotrade': 'bbb222',
                        'autostraggle': 'ccc333',
                    }},
                    f,
                    allow_unicode=True,
                    sort_keys=False,
                )

            cfg = {
                'compat_lock_path': lock_path,
                'compat_lock_enforce': True,
            }
            logger = _FakeLogger()
            with patch('merged_startup_checks._resolve_repo_root', return_value=tmp), \
                    patch('merged_startup_checks._git_short_commit', return_value='zzz999'), \
                    patch('merged_startup_checks._git_is_dirty', return_value=False):
                self.assertFalse(audit_repo_compat_lock(cfg, logger))
            self.assertTrue(any(level == 'error' for level, _ in logger.logs))

    def test_match_allows_startup(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = os.path.join(tmp, 'compat_lock.yaml')
            with open(lock_path, 'w', encoding='utf-8') as f:
                yaml.safe_dump(
                    {'expected_commits': {
                        'autoctp': 'abc123',
                        'autotrade': 'abc123',
                        'autostraggle': 'abc123',
                    }},
                    f,
                    allow_unicode=True,
                    sort_keys=False,
                )

            cfg = {
                'compat_lock_path': lock_path,
                'compat_lock_enforce': True,
            }
            logger = _FakeLogger()
            with patch('merged_startup_checks._resolve_repo_root', return_value=tmp), \
                    patch('merged_startup_checks._git_short_commit', return_value='abc123'), \
                    patch('merged_startup_checks._git_is_dirty', return_value=False):
                self.assertTrue(audit_repo_compat_lock(cfg, logger))


if __name__ == '__main__':
    unittest.main()
