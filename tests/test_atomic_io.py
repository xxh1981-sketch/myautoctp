"""atomic_io unit tests"""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import atomic_io
from atomic_io import atomic_write_text


class TestAtomicIo(unittest.TestCase):

    def test_atomic_write_creates_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'nested', 'data.csv')
            atomic_write_text(path, 'a,b\n1,2\n')
            with open(path, encoding='utf-8') as f:
                self.assertEqual(f.read(), 'a,b\n1,2\n')

    def test_atomic_write_replaces_existing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'x.txt')
            atomic_write_text(path, 'old')
            atomic_write_text(path, 'new')
            with open(path, encoding='utf-8') as f:
                self.assertEqual(f.read(), 'new')


class TestReplaceRetry(unittest.TestCase):
    """模拟 Windows WinError 5：前几次 os.replace 抛 PermissionError，重试后成功。"""

    def test_transient_permission_error_retried(self):
        real_replace = os.replace
        calls = {'n': 0}

        def flaky_replace(src, dst):
            calls['n'] += 1
            if calls['n'] < 3:
                raise PermissionError(5, '拒绝访问')
            return real_replace(src, dst)

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'ledger.json')
            with mock.patch.object(atomic_io.os, 'replace', flaky_replace), \
                    mock.patch.object(atomic_io.time, 'sleep', lambda *_: None):
                atomic_write_text(path, '{"ok": 1}')
            self.assertEqual(calls['n'], 3)
            with open(path, encoding='utf-8') as f:
                self.assertEqual(f.read(), '{"ok": 1}')

    def test_persistent_permission_error_raises_and_cleans_tmp(self):
        def always_fail(src, dst):
            raise PermissionError(5, '拒绝访问')

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'ledger.json')
            with mock.patch.object(atomic_io.os, 'replace', always_fail), \
                    mock.patch.object(atomic_io.time, 'sleep', lambda *_: None):
                with self.assertRaises(PermissionError):
                    atomic_write_text(path, 'x')
            # 重试用尽后抛出，且不残留 .tmp_ 文件、目标文件未创建。
            leftovers = [n for n in os.listdir(tmp) if n.startswith('.tmp_')]
            self.assertEqual(leftovers, [])
            self.assertFalse(os.path.exists(path))


if __name__ == '__main__':
    unittest.main()
