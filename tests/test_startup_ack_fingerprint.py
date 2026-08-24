"""startup_ack_fingerprint unit tests"""

import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from startup_ack_fingerprint import (
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


def _ledger_cfg(tmp: str) -> dict:
    spread = os.path.join(tmp, 'spread_positions.csv')
    strangle = os.path.join(tmp, 'strangle_positions.csv')
    ledger = os.path.join(tmp, 'ledger_strangle.json')
    with open(spread, 'w', encoding='utf-8') as f:
        f.write('instrument,volume\n')
    with open(strangle, 'w', encoding='utf-8') as f:
        f.write('instrument,volume\n')
    base = {
        'positions': [],
        'leg_claims': {},
        'unmatched_legs': [],
        'cooldowns': [],
        'daily_groups': {},
        'daily_buy_amount': {},
        'open_halted': False,
        'open_halt_reason': '',
    }
    with open(ledger, 'w', encoding='utf-8') as f:
        json.dump(base, f, ensure_ascii=False, indent=2)
    return {
        'dual_strategy': {
            'startup_ack_file': os.path.join(tmp, 'position_startup_ack.txt'),
            'spread_positions_csv': spread,
            'strangle_positions_csv': strangle,
            'startup_ack_track_ledger_files': True,
        },
        'strangle': {'ledger_path': ledger},
    }


class TestLedgerTruthNormalization(unittest.TestCase):

    def test_unmatched_retry_metadata_not_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _ledger_cfg(tmp)
            ledger = cfg['strangle']['ledger_path']
            with open(ledger, 'r', encoding='utf-8') as f:
                data = json.load(f)
            data['unmatched_legs'] = [{
                'symbol': 'm',
                'month': '2701',
                'volume': 1,
                'kind': 'inferred_single',
                'b_retry_count': 0,
                'combo_id': 'strangle-M-close-1',
                'base_future_price': 3098.0,
                'leg': {'inst': 'm2701-P-2900', 'label': 'put'},
                'stage': 'close',
                'action': 'SELL',
            }]
            with open(ledger, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            save_startup_ack_fingerprint(cfg)
            ok, reasons = check_startup_ack_fingerprint(cfg)
            self.assertTrue(ok, reasons)
            data['unmatched_legs'][0]['b_retry_count'] = 10
            data['unmatched_legs'][0]['combo_id'] = 'strangle-M-close-999'
            data['unmatched_legs'][0]['base_future_price'] = 3200.0
            with open(ledger, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            ok, reasons = check_startup_ack_fingerprint(cfg)
            self.assertTrue(ok, reasons)

    def test_leg_claims_case_insensitive_stable(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _ledger_cfg(tmp)
            ledger = cfg['strangle']['ledger_path']
            with open(ledger, 'w', encoding='utf-8') as f:
                json.dump({
                    'positions': [], 'leg_claims': {'m2701-C-3300': 1},
                    'unmatched_legs': [],
                }, f)
            save_startup_ack_fingerprint(cfg)
            with open(ledger, 'w', encoding='utf-8') as f:
                json.dump({
                    'positions': [], 'leg_claims': {'M2701-C-3300': 1},
                    'unmatched_legs': [],
                }, f)
            ok, reasons = check_startup_ack_fingerprint(cfg)
            self.assertTrue(ok, reasons)

    def test_real_leg_claim_change_is_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _ledger_cfg(tmp)
            ledger = cfg['strangle']['ledger_path']
            save_startup_ack_fingerprint(cfg)
            with open(ledger, 'w', encoding='utf-8') as f:
                json.dump({
                    'positions': [], 'leg_claims': {'M2701-C-3300': 2},
                    'unmatched_legs': [],
                }, f)
            ok, reasons = check_startup_ack_fingerprint(cfg)
            self.assertFalse(ok)
            self.assertTrue(any('ledger_strangle' in r for r in reasons))


if __name__ == '__main__':
    unittest.main()
