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
        """子进程尝试拿锁应失败（实测跨进程行为，依赖 OS 锁）。"""
        if os.name == 'nt':
            self.skipTest('Windows msvcrt.locking 在子进程模拟不便测；'
                          '在 CI/Linux 上验证 fcntl.flock 跨进程行为即可。')
        process_guard.acquire_singleton(pid_path=self.pid_path)
        import multiprocessing as mp

        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

        def _try_acquire(path, root, q):
            import sys
            if root not in sys.path:
                sys.path.insert(0, root)
            import process_guard as g
            g._HELD_FD = None
            g._HELD_PATH = None
            try:
                g.acquire_singleton(pid_path=path)
                q.put('OK')
            except g.AlreadyRunningError as e:
                q.put(f'BLOCKED:{e}')
            except Exception as e:
                q.put(f'ERR:{e}')

        ctx = mp.get_context('fork' if os.name != 'nt' else 'spawn')
        q = ctx.Queue()
        p = ctx.Process(target=_try_acquire, args=(self.pid_path, repo_root, q))
        p.start()
        p.join(10)
        result = q.get(timeout=2)
        self.assertTrue(
            result.startswith('BLOCKED'),
            f'second process should be blocked, got: {result}',
        )


if __name__ == '__main__':
    unittest.main()
