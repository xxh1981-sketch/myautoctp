"""recover_holdback_into_ledger crash-safety tests."""

import json
import os
import sys
import tempfile
import threading
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ctp_bootstrap  # noqa: F401

import strangle_close_only_holdback as hb


class FakeLedger:
    """Minimal ledger stub: enough for holdback recovery to read/write
    ``_data['unmatched_legs']`` under ``_lock`` and call ``_save``."""

    def __init__(self, path, data=None, save_raises=False):
        self.path = path
        self._data = data or {'unmatched_legs': []}
        self._lock = threading.RLock()
        self._save_raises = save_raises
        self.save_calls = 0

    def _save(self):
        self.save_calls += 1
        if self._save_raises:
            raise IOError('disk full simulated')


class TestRecoverHoldback(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.ledger_path = os.path.join(self.tmp, 'ledger.json')
        self.holdback_path = os.path.join(self.tmp, '_close_only_holdback.json')

    def _write_holdback(self, items):
        with open(self.holdback_path, 'w', encoding='utf-8') as f:
            json.dump({'items': items}, f)

    def test_no_holdback_file_returns_zero(self):
        ledger = FakeLedger(self.ledger_path)
        self.assertEqual(hb.recover_holdback_into_ledger(ledger), 0)

    def test_merge_new_items_removes_file(self):
        ledger = FakeLedger(self.ledger_path)
        self._write_holdback([
            {'symbol': 'SA', 'month': '609', 'kind': 'open', 'leg': {'inst': 'SA609C1000'}},
        ])
        logger = MagicMock()
        added = hb.recover_holdback_into_ledger(ledger, logger=logger)
        self.assertEqual(added, 1)
        self.assertEqual(len(ledger._data['unmatched_legs']), 1)
        self.assertFalse(os.path.isfile(self.holdback_path),
                         'holdback 文件应在成功合并后被删除')

    def test_save_failure_keeps_file_for_retry(self):
        """ledger._save 抛错时，holdback 文件必须保留以便下次启动重试。"""
        ledger = FakeLedger(self.ledger_path, save_raises=True)
        self._write_holdback([
            {'symbol': 'SA', 'month': '609', 'kind': 'open', 'leg': {'inst': 'SA609C1000'}},
        ])
        logger = MagicMock()
        with self.assertRaises(IOError):
            hb.recover_holdback_into_ledger(ledger, logger=logger)
        self.assertTrue(
            os.path.isfile(self.holdback_path),
            'save 失败时 holdback 文件必须保留',
        )
        error_msgs = [c.args[0] for c in logger.error.call_args_list]
        self.assertTrue(
            any('holdback 合并写盘失败' in m for m in error_msgs),
            f'expected error log, got {error_msgs}',
        )

    def test_dedupe_keeps_idempotent(self):
        """同一笔 leg 已经在 ledger 中，重复合并 added=0 且文件被删。"""
        existing = [
            {'symbol': 'SA', 'month': '609', 'kind': 'open', 'leg': {'inst': 'SA609C1000'}},
        ]
        ledger = FakeLedger(self.ledger_path, data={'unmatched_legs': existing})
        self._write_holdback(existing)
        added = hb.recover_holdback_into_ledger(ledger)
        self.assertEqual(added, 0)
        self.assertFalse(os.path.isfile(self.holdback_path),
                         '即使无新增也应删除已处理过的 holdback')

    def test_empty_payload_removed(self):
        ledger = FakeLedger(self.ledger_path)
        self._write_holdback([])
        added = hb.recover_holdback_into_ledger(ledger)
        self.assertEqual(added, 0)
        self.assertFalse(os.path.isfile(self.holdback_path))

    def test_corrupt_file_removed_no_raise(self):
        ledger = FakeLedger(self.ledger_path)
        with open(self.holdback_path, 'w', encoding='utf-8') as f:
            f.write('{not valid json')
        logger = MagicMock()
        added = hb.recover_holdback_into_ledger(ledger, logger=logger)
        self.assertEqual(added, 0)
        self.assertFalse(os.path.isfile(self.holdback_path))


if __name__ == '__main__':
    unittest.main()
