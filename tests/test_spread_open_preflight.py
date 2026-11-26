"""spread_open_preflight 单元测试。"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from spread_open_preflight import (  # noqa: E402
    process_spread_symbol,
    should_block_spread_open_empty_book_ctp_residual,
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

    @patch('spread_open_preflight._spread_has_ledger_claims', return_value=False)
    @patch('spread_open_preflight._count_a_from_positions', return_value=9)
    def test_open_clip_headroom_allows_one_combo(self, _a, _claims):
        """vol_of_combo=10 但仅剩 1 手 A 额度时，应按 headroom 计 1 而非 10。"""
        conn = MagicMock()
        conn._normalize_month = MagicMock(return_value='2609')
        conn.position_tracker = MagicMock()
        conn.position_tracker.get_positions_for_symbol.return_value = []
        config = {
            'dual_strategy': {'exclude_strangle_from_spread_positions': True},
            'open_clip': {'enabled': True, 'max_groups': 8},
            'A_POSITION_LIMIT_RATIO': 1,
            'A_target_volume_per_combo': 1,
        }
        item = {'future': 'sc', 'month': '2609', 'vol_of_combo': 10}
        from spread_open_preflight import estimate_spread_a_headroom, should_skip_spread_open_only_scan
        a_cur, a_lim, planned = estimate_spread_a_headroom(conn, item, config)
        self.assertEqual(a_cur, 9)
        self.assertEqual(a_lim, 10)
        self.assertEqual(planned, 1)
        self.assertFalse(should_skip_spread_open_only_scan(
            conn, item, config, spread_open_ok=True,
        ))


class TestBlockEmptyBookCtpResidual(unittest.TestCase):

    def _item(self):
        return {'future': 'sc', 'month': '2609', 'vol_of_combo': 1}

    def test_not_block_when_halted(self):
        conn = MagicMock()
        self.assertFalse(
            should_block_spread_open_empty_book_ctp_residual(
                conn, self._item(), spread_open_ok=False,
            ),
        )

    @patch('spread_open_preflight._spread_has_ledger_claims', return_value=True)
    def test_not_block_when_ledger_has_claims(self, _claims):
        conn = MagicMock()
        self.assertFalse(
            should_block_spread_open_empty_book_ctp_residual(
                conn, self._item(), spread_open_ok=True,
            ),
        )

    @patch('spread_open_preflight._spread_has_ledger_claims', return_value=False)
    @patch('spread_position_sync.spread_ctp_has_residual', return_value=True)
    def test_block_when_empty_book_and_ctp_residual(self, _res, _claims):
        conn = MagicMock()
        self.assertTrue(
            should_block_spread_open_empty_book_ctp_residual(
                conn, self._item(), spread_open_ok=True,
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

    @patch('spread_open_preflight.should_block_spread_open_empty_book_ctp_residual', return_value=False)
    @patch('spread_open_preflight.should_skip_spread_open_only_scan', return_value=False)
    def test_delegates_to_process_symbol(self, _skip, _block):
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

    @patch('spread_open_preflight.should_block_spread_open_empty_book_ctp_residual', return_value=True)
    @patch('spread_open_preflight.should_skip_spread_open_only_scan', return_value=False)
    def test_empty_book_residual_blocks_process_symbol(self, _skip, _block):
        conn = MagicMock()
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


if __name__ == '__main__':
    unittest.main()
