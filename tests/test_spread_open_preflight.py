"""spread_open_preflight 单元测试。"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from spread_open_preflight import (  # noqa: E402
    process_spread_symbol,
    should_skip_spread_open_only_scan,
)


class TestShouldSkipSpreadOpenOnlyScan(unittest.TestCase):

    def _item(self):
        return {'future': 'sc', 'month': '2609', 'vol_of_combo': 1}

    def test_not_skip_when_spread_open_halted(self):
        conn = MagicMock()
        self.assertFalse(
            should_skip_spread_open_only_scan(
                conn, self._item(), {'dual_strategy': {}}, spread_open_ok=False,
            ),
        )

    @patch('spread_open_preflight._spread_has_ledger_claims', return_value=True)
    def test_not_skip_when_ledger_has_claims(self, _claims):
        conn = MagicMock()
        self.assertFalse(
            should_skip_spread_open_only_scan(
                conn,
                self._item(),
                {'dual_strategy': {'exclude_strangle_from_spread_positions': True}},
                spread_open_ok=True,
            ),
        )

    @patch('spread_open_preflight._spread_has_ledger_claims', return_value=False)
    @patch('spread_open_preflight.estimate_spread_a_headroom', return_value=(1, 1, 1))
    def test_skip_when_a_would_exceed(self, _est, _claims):
        conn = MagicMock()
        self.assertTrue(
            should_skip_spread_open_only_scan(
                conn,
                self._item(),
                {'dual_strategy': {'exclude_strangle_from_spread_positions': True}},
                spread_open_ok=True,
            ),
        )

    @patch('spread_open_preflight._spread_has_ledger_claims', return_value=False)
    @patch('spread_open_preflight.estimate_spread_a_headroom', return_value=(0, 1, 1))
    def test_not_skip_when_headroom_ok(self, _est, _claims):
        conn = MagicMock()
        self.assertFalse(
            should_skip_spread_open_only_scan(
                conn,
                self._item(),
                {'dual_strategy': {'exclude_strangle_from_spread_positions': True}},
                spread_open_ok=True,
            ),
        )


class TestProcessSpreadSymbol(unittest.TestCase):

    @patch('spread_open_preflight.should_skip_spread_open_only_scan', return_value=True)
    @patch('spread_open_preflight.estimate_spread_a_headroom', return_value=(1, 1, 1))
    def test_skip_does_not_call_process_symbol(self, _est, _skip):
        conn = MagicMock()
        conn._runtime_state = {}
        logger = MagicMock()
        mock_proc = MagicMock()
        fake_ap = MagicMock()
        fake_ap.process_symbol = mock_proc
        with patch.dict(sys.modules, {'auto_processor': fake_ap}):
            self.assertFalse(
                process_spread_symbol(
                    conn,
                    {'future': 'sc', 'month': '2609', 'vol_of_combo': 1},
                    MagicMock(),
                    {},
                    logger,
                    spread_open_ok=True,
                ),
            )
        mock_proc.assert_not_called()

    @patch('spread_open_preflight.should_skip_spread_open_only_scan', return_value=False)
    def test_delegates_to_process_symbol(self, _skip):
        conn = MagicMock()
        logger = MagicMock()
        mock_proc = MagicMock(return_value=True)
        fake_ap = MagicMock()
        fake_ap.process_symbol = mock_proc
        item = {'future': 'sc', 'month': '2609', 'vol_of_combo': 1}
        with patch.dict(sys.modules, {'auto_processor': fake_ap}):
            self.assertTrue(
                process_spread_symbol(
                    conn, item, MagicMock(), {}, logger, spread_open_ok=True,
                ),
            )
        mock_proc.assert_called_once()


if __name__ == '__main__':
    unittest.main()
