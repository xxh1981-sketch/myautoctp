"""Periodic reconcile respects startup external-position ack."""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ctp_bootstrap  # noqa: F401

from account_decomposition import register_acknowledged_external


class TestExternalAckReconcile(unittest.TestCase):

    def test_spread_reconcile_skips_halt_when_ack_matches(self):
        from spread_reconcile import reconcile_spread_positions

        conn = MagicMock()
        conn._runtime_state = {}
        conn._normalize_month = lambda sym, month: month
        store = MagicMock()
        store.list_leg_claims.return_value = {'lc2609-C-198000': 1}
        config = {
            'dual_strategy': {
                'auto_sync_spread_positions_csv': False,
                'exclude_strangle_from_spread_reconcile': True,
            },
            'spread_tradeinfo': [{'future': 'lc', 'month': '2609'}],
        }
        register_acknowledged_external(
            config, {'LC2609-C-198000': 2}, conn, persist=False,
        )

        positions = [
            {
                'instrument': 'lc2609-C-198000',
                'direction': '2',
                'position': 3,
            },
        ]
        with patch('spread_reconcile.store_from_conn', return_value=store):
            with patch(
                'spread_reconcile.ledger_spread_signed_claims',
                return_value={'LC2609-C-198000': 1},
            ):
                with patch(
                    'spread_reconcile.ctp_spread_signed_claims',
                    return_value={'lc2609-C-198000': 3},
                ):
                    with patch(
                        'spread_reconcile._strangle_long_calls_for_spread',
                        return_value={},
                    ):
                        halt, issues = reconcile_spread_positions(
                            conn,
                            config['spread_tradeinfo'],
                            None,
                            config=config,
                            positions=positions,
                        )
        self.assertFalse(halt)
        self.assertTrue(any('已确认外部仓' in i for i in issues))


if __name__ == '__main__':
    unittest.main()
