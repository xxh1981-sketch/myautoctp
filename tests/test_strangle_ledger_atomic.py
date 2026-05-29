"""StrangleLedger atomic-save patch tests."""

import json
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ctp_bootstrap  # noqa: F401

from strangle_ledger_atomic import install_atomic_save, get_install_error
from straggle_ledger import StrangleLedger


class TestStrangleLedgerAtomicSave(unittest.TestCase):

    def setUp(self):
        install_atomic_save()

    def test_install_returns_true_and_no_error(self):
        """安装成功必须返回 True 且无错误信息（供 merged_main 决定是否 fail-fast）。"""
        self.assertTrue(install_atomic_save())
        self.assertEqual(get_install_error(), '')

    def test_save_writes_full_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'sl.json')
            led = StrangleLedger(path)
            led.set_leg_claims({'SA609C2400': 3})
            led.add_unmatched_leg({'symbol': 'sa', 'month': '609', 'kind': 'k1'})
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            self.assertEqual(data['leg_claims']['SA609C2400'], 3)
            self.assertEqual(len(data['unmatched_legs']), 1)

    def test_save_uses_temp_file_and_replace(self):
        """原子写：写入期间目录里不应残留 .tmp_ 文件。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'sl.json')
            led = StrangleLedger(path)
            led.set_leg_claims({'SA609C2400': 1})
            tmps = [n for n in os.listdir(tmp) if n.startswith('.tmp_')]
            self.assertEqual(tmps, [])

    def test_concurrent_save_no_corruption(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'sl.json')
            led = StrangleLedger(path)

            def worker(n):
                for i in range(20):
                    led.record_buy_amount(float(n * 100 + i))

            threads = [threading.Thread(target=worker, args=(k,)) for k in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            self.assertIn('daily_buy_amount', data)


if __name__ == '__main__':
    unittest.main()
