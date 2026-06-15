"""position_csv_integrity 单元测试。"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from position_csv_integrity import (
    apply_position_csv_integrity_halt,
    validate_position_csvs,
    validate_spread_positions_csv,
)


class TestPositionCsvIntegrity(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_empty_file_is_valid(self):
        path = os.path.join(self.tmp.name, 'spread.csv')
        open(path, 'w', encoding='utf-8').close()
        ok, err = validate_spread_positions_csv(path)
        self.assertTrue(ok)
        self.assertEqual(err, '')

    def test_missing_file_is_valid(self):
        ok, err = validate_spread_positions_csv(
            os.path.join(self.tmp.name, 'missing.csv'),
        )
        self.assertTrue(ok)

    def test_corrupt_line_fails(self):
        path = os.path.join(self.tmp.name, 'bad.csv')
        with open(path, 'w', encoding='utf-8') as f:
            f.write('SA609C2200\n')
        ok, err = validate_spread_positions_csv(path)
        self.assertFalse(ok)
        self.assertIn('line', err.lower())

    def test_valid_spread_csv(self):
        path = os.path.join(self.tmp.name, 'good.csv')
        with open(path, 'w', encoding='utf-8') as f:
            f.write('instrument,volume\n')
            f.write('SA609C2200,2\n')
        ok, err = validate_spread_positions_csv(path)
        self.assertTrue(ok)

    def test_apply_halt_on_corrupt(self):
        spread_path = os.path.join(self.tmp.name, 'spread.csv')
        with open(spread_path, 'w', encoding='utf-8') as f:
            f.write('instrument,volume\n')
            f.write('SA609C2200,notint\n')
        config = {
            'dual_strategy': {
                'spread_positions_csv': spread_path,
                'strangle_positions_csv': os.path.join(self.tmp.name, 'str.csv'),
            },
            'position_csv_integrity_enabled': True,
        }
        conn = type('C', (), {})()
        conn._runtime_state = {}
        active = apply_position_csv_integrity_halt(conn, config, logger=None)
        self.assertTrue(active)
        self.assertTrue(conn._runtime_state['_position_csv_halt_open'])
        errors = validate_position_csvs(config)
        self.assertTrue(errors)


if __name__ == '__main__':
    unittest.main()
