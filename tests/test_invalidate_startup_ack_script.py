"""scripts/invalidate_startup_ack.py tests"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestInvalidateStartupAckScript(unittest.TestCase):

    def _cfg(self, tmp: str) -> dict:
        return {
            'dual_strategy': {
                'startup_ack_file': os.path.join(tmp, 'position_startup_ack.txt'),
                'external_positions_ack_file': os.path.join(tmp, 'external_positions_ack.json'),
                'spread_trade_journal': os.path.join(tmp, 'spread_trade_journal.jsonl'),
                'strangle_trade_journal': os.path.join(tmp, 'strangle_trade_journal.jsonl'),
                'fill_ledger_journal': os.path.join(tmp, 'fill_ledger_journal.jsonl'),
                'fill_ledger_csv': os.path.join(tmp, 'fill_ledger.csv'),
                'strangle_positions_csv': os.path.join(tmp, 'strangle_positions.csv'),
            },
            'strangle': {
                'ledger_path': os.path.join(tmp, 'ledger_strangle.json'),
            },
        }

    def _touch(self, path: str, content: str = 'x\n') -> None:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(content)

    def test_default_only_removes_ack_related_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._cfg(tmp)
            ack = cfg['dual_strategy']['startup_ack_file']
            meta = ack + '.meta.json'
            ext = cfg['dual_strategy']['external_positions_ack_file']
            spread_j = cfg['dual_strategy']['spread_trade_journal']
            fill_csv = cfg['dual_strategy']['fill_ledger_csv']

            self._touch(ack, 'confirmed 2026-05-29\n')
            self._touch(meta, '{}\n')
            self._touch(ext, '{}\n')
            self._touch(spread_j, '{}\n')
            self._touch(fill_csv, 'instrument,price\n')

            import scripts.invalidate_startup_ack as mod
            with patch.object(mod, 'load_merged_config', return_value=cfg), \
                 patch.object(sys, 'argv', ['invalidate_startup_ack.py']):
                rc = mod.main()

            self.assertEqual(rc, 0)
            self.assertFalse(os.path.isfile(ack))
            self.assertFalse(os.path.isfile(meta))
            self.assertFalse(os.path.isfile(ext))
            self.assertTrue(os.path.isfile(spread_j))
            self.assertTrue(os.path.isfile(fill_csv))

    def test_account_switch_removes_journals_and_fill_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._cfg(tmp)
            spread_j = cfg['dual_strategy']['spread_trade_journal']
            spread_shard = spread_j.replace('.jsonl', '-20260529.jsonl')
            strangle_j = cfg['dual_strategy']['strangle_trade_journal']
            fill_j = cfg['dual_strategy']['fill_ledger_journal']
            fill_csv = cfg['dual_strategy']['fill_ledger_csv']
            positions_csv = cfg['dual_strategy']['strangle_positions_csv']
            ledger_path = cfg['strangle']['ledger_path']

            for path in (
                cfg['dual_strategy']['startup_ack_file'],
                cfg['dual_strategy']['startup_ack_file'] + '.meta.json',
                cfg['dual_strategy']['external_positions_ack_file'],
                spread_j,
                spread_shard,
                strangle_j,
                fill_j,
                fill_csv,
                positions_csv,
                ledger_path,
            ):
                self._touch(path, '{}\n')

            import scripts.invalidate_startup_ack as mod
            with patch.object(mod, 'load_merged_config', return_value=cfg), \
                 patch(
                     'import_strangle_positions.import_csv_to_ledger',
                     return_value=0,
                 ) as mock_reset, \
                 patch.object(sys, 'argv', ['invalidate_startup_ack.py', '--account-switch']):
                rc = mod.main()

            self.assertEqual(rc, 0)
            self.assertFalse(os.path.isfile(spread_j))
            self.assertFalse(os.path.isfile(spread_shard))
            self.assertFalse(os.path.isfile(strangle_j))
            self.assertFalse(os.path.isfile(fill_j))
            self.assertFalse(os.path.isfile(fill_csv))
            mock_reset.assert_called_once()


if __name__ == '__main__':
    unittest.main()
