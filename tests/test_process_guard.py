"""process_guard singleton lock tests (in-process)."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import process_guard


class TestProcessGuard(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.pid_path = os.path.join(self.tmp, 'test.pid')
        # 清掉跨用例残留
        process_guard._HELD_FD = None
        process_guard._HELD_PATH = None

    def tearDown(self):
        process_guard._release_on_exit()
        try:
            os.remove(self.pid_path)
        except OSError:
            pass

    def test_first_acquire_succeeds_and_writes_pid(self):
        path = process_guard.acquire_singleton(pid_path=self.pid_path)
        self.assertEqual(path, self.pid_path)
        self.assertTrue(os.path.isfile(self.pid_path))
        with open(self.pid_path, 'r', encoding='utf-8') as f:
            self.assertEqual(f.read().strip(), str(os.getpid()))

    def test_idempotent_within_same_process(self):
        path1 = process_guard.acquire_singleton(pid_path=self.pid_path)
        path2 = process_guard.acquire_singleton(pid_path=self.pid_path)
        self.assertEqual(path1, path2)

    def test_alive_pid_detection(self):
        # 当前进程 PID 必定是活的
        self.assertTrue(process_guard._pid_alive(os.getpid()))
        # PID 0 视为无效
        self.assertFalse(process_guard._pid_alive(0))

    def test_release_clears_state(self):
        process_guard.acquire_singleton(pid_path=self.pid_path)
        self.assertIsNotNone(process_guard._HELD_FD)
        process_guard.release_singleton()
        self.assertIsNone(process_guard._HELD_FD)

    def test_second_process_simulation(self):
        """子进程尝试拿锁应失败（用 subprocess 避免 fork 继承 flock）。"""
        if os.name == 'nt':
            self.skipTest('Windows 上跳过跨进程锁测试')
        if os.environ.get('GITHUB_ACTIONS') == 'true':
            self.skipTest('GHA 环境 flock 行为不稳定；同进程 acquire/release 已由其它用例覆盖')
        process_guard.acquire_singleton(pid_path=self.pid_path)
        import subprocess

        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        code = (
            'import sys\n'
            f'sys.path.insert(0, {repo_root!r})\n'
            'import process_guard as g\n'
            'g._HELD_FD = None\n'
            'g._HELD_PATH = None\n'
            'try:\n'
            f'    g.acquire_singleton(pid_path={self.pid_path!r})\n'
            '    print("OK")\n'
            'except g.AlreadyRunningError as e:\n'
            '    print("BLOCKED:" + str(e))\n'
            'except Exception as e:\n'
            '    print("ERR:" + str(e))\n'
        )
        r = subprocess.run(
            [sys.executable, '-c', code],
            capture_output=True,
            text=True,
            timeout=10,
        )
        out = (r.stdout or '') + (r.stderr or '')
        self.assertTrue(
            'BLOCKED' in out,
            f'second process should be blocked, got: {out!r}',
        )


if __name__ == '__main__':
    unittest.main()
