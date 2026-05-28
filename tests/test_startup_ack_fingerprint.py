"""startup_ack_fingerprint unit tests"""

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from startup_ack_fingerprint import (
    build_ledger_fingerprint,
    check_startup_ack_fingerprint,
    invalidate_startup_ack_files,
    save_startup_ack_fingerprint,
    startup_ack_meta_path,
)


def _base_config(tmp: str, ack_name: str = 'position_startup_ack.txt') -> dict:
    spread = os.path.join(tmp, 'spread_positions.csv')
    strangle = os.path.join(tmp, 'strangle_positions.csv')
    ledger = os.path.join(tmp, 'ledger_strangle.json')
    for path in (spread, strangle, ledger):
        with open(path, 'w', encoding='utf-8') as f:
            f.write('x\n')
    return {
        'dual_strategy': {
            'startup_ack_file': os.path.join(tmp, ack_name),
            'spread_positions_csv': spread,
            'strangle_positions_csv': strangle,
            'startup_ack_track_ledger_files': True,
        },
        'strangle': {'ledger_path': ledger},
    }


class TestStartupAckFingerprint(unittest.TestCase):

    def test_save_and_check_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _base_config(tmp)
            save_startup_ack_fingerprint(cfg)
            ok, reasons = check_startup_ack_fingerprint(cfg)
            self.assertTrue(ok, reasons)
            self.assertTrue(os.path.isfile(startup_ack_meta_path(cfg)))

    def test_modified_csv_fails_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _base_config(tmp)
            save_startup_ack_fingerprint(cfg)
            spread = cfg['dual_strategy']['spread_positions_csv']
            time.sleep(0.05)
            with open(spread, 'a', encoding='utf-8') as f:
                f.write('y\n')
            ok, reasons = check_startup_ack_fingerprint(cfg)
            self.assertFalse(ok)
            self.assertTrue(any('spread_positions' in r for r in reasons))

    def test_missing_meta_fails_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _base_config(tmp)
            ok, reasons = check_startup_ack_fingerprint(cfg)
            self.assertFalse(ok)
            self.assertTrue(any('指纹' in r for r in reasons))

    def test_invalidate_removes_ack_meta_external(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _base_config(tmp)
            ack = cfg['dual_strategy']['startup_ack_file']
            ext = os.path.join(tmp, 'external_positions_ack.json')
            cfg['dual_strategy']['external_positions_ack_file'] = ext
            with open(ack, 'w', encoding='utf-8') as f:
                f.write('confirmed\n')
            with open(ext, 'w', encoding='utf-8') as f:
                f.write('{}')
            save_startup_ack_fingerprint(cfg)
            removed = invalidate_startup_ack_files(cfg)
            self.assertIn(ack, removed)
            self.assertFalse(os.path.isfile(startup_ack_meta_path(cfg)))


if __name__ == '__main__':
    unittest.main()
