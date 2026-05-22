"""External-position JSON persist / restore on unattended startup."""

import os
import sys
import tempfile
import unittest
from datetime import date
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ctp_bootstrap  # noqa: F401

from account_decomposition import (
    external_ack_path,
    load_external_ack_file,
    register_acknowledged_external,
    restore_external_ack_from_file,
    save_external_ack_file,
)
from merged_startup_ack import require_startup_position_ack


class TestExternalAckPersist(unittest.TestCase):

    def _config(self, tmp, **dual_kw):
        ack = os.path.join(tmp, 'position_startup_ack.txt')
        ext = os.path.join(tmp, 'external_positions_ack.json')
        dual = {
            'startup_ack_file': ack,
            'external_positions_ack_file': ext,
            'external_ack_persist': True,
            'external_ack_strict_on_restore': True,
            **dual_kw,
        }
        return {'dual_strategy': dual}

    def test_save_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._config(tmp)
            save_external_ack_file(cfg, {'LC2609-C-198000': 2})
            loaded = load_external_ack_file(cfg)
            self.assertEqual(loaded, {'LC2609-C-198000': 2})

    def test_register_persists_and_removes(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._config(tmp)
            register_acknowledged_external(cfg, {'LC2609-C-198000': 1})
            self.assertTrue(os.path.isfile(external_ack_path(cfg)))
            register_acknowledged_external(cfg, {})
            self.assertFalse(os.path.isfile(external_ack_path(cfg)))

    def test_restore_match_registers(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._config(tmp)
            cfg['spread_tradeinfo'] = [{'future': 'lc', 'month': '2609'}]
            cfg['strangle_tradeinfo'] = []
            save_external_ack_file(cfg, {'LC2609-C-198000': 1})
            conn = MagicMock()
            conn._runtime_state = {}
            conn._normalize_month = lambda sym, month: month
            ledger = MagicMock()
            store = MagicMock()
            store.list_leg_claims.return_value = {'lc2609-C-198000': 1}
            with patch(
                'account_decomposition.query_ctp_signed_positions',
                return_value={'lc2609-C-198000': 2},
            ):
                with patch(
                    'spread_ledger.store_from_conn',
                    return_value=store,
                ):
                    with patch(
                        'account_decomposition.merge_strangle_owned_volumes',
                        return_value={},
                    ):
                        ok = restore_external_ack_from_file(
                            cfg, conn, ledger, None,
                        )
            self.assertTrue(ok)
            self.assertTrue(cfg['_external_positions_acknowledged'])

    def test_restore_mismatch_fails_strict(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._config(tmp)
            cfg['spread_tradeinfo'] = [{'future': 'lc', 'month': '2609'}]
            cfg['strangle_tradeinfo'] = []
            save_external_ack_file(cfg, {'LC2609-C-198000': 99})
            conn = MagicMock()
            conn._normalize_month = lambda sym, month: month
            ledger = MagicMock()
            store = MagicMock()
            store.list_leg_claims.return_value = {'lc2609-C-198000': 1}
            with patch(
                'account_decomposition.query_ctp_signed_positions',
                return_value={'lc2609-C-198000': 2},
            ):
                with patch('spread_ledger.store_from_conn', return_value=store):
                    with patch(
                        'account_decomposition.merge_strangle_owned_volumes',
                        return_value={},
                    ):
                        ok = restore_external_ack_from_file(
                            cfg, conn, ledger, None,
                        )
            self.assertFalse(ok)

    def test_auto_restart_restores_external_via_ack_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._config(tmp)
            cfg['_manual_start'] = False
            cfg['_auto_restart'] = True
            cfg['spread_tradeinfo'] = [{'future': 'lc', 'month': '2609'}]
            cfg['strangle_tradeinfo'] = []
            ack = cfg['dual_strategy']['startup_ack_file']
            with open(ack, 'w', encoding='utf-8') as f:
                f.write(f'confirmed {date.today().isoformat()}\n')
            save_external_ack_file(cfg, {'LC2609-C-198000': 1})

            conn = MagicMock()
            conn._runtime_state = {}
            conn._normalize_month = lambda sym, month: month
            ledger = MagicMock()
            ledger.list_positions.return_value = []
            ledger.list_leg_claims.return_value = {}
            ledger.list_unmatched_legs.return_value = []
            store = MagicMock()
            store.list_leg_claims.return_value = {'lc2609-C-198000': 1}
            logger = MagicMock()

            with patch(
                'account_decomposition.query_ctp_signed_positions',
                return_value={'lc2609-C-198000': 2},
            ):
                with patch('spread_ledger.store_from_conn', return_value=store):
                    with patch(
                        'account_decomposition.merge_strangle_owned_volumes',
                        return_value={},
                    ):
                        ok = require_startup_position_ack(
                            cfg, logger, ledger, conn=conn,
                        )
            self.assertTrue(ok)
            self.assertTrue(cfg['_external_positions_acknowledged'])

    def test_auto_confirm_does_not_write_external_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._config(tmp)
            ext_path = external_ack_path(cfg)
            ledger = MagicMock()
            ledger.list_positions.return_value = []
            ledger.list_leg_claims.return_value = {}
            logger = MagicMock()
            env = {'AUTOCTP_CONFIRM': 'yes'}
            with patch.dict(os.environ, env, clear=False):
                with patch(
                    'account_decomposition.restore_external_ack_from_file',
                    return_value=True,
                ):
                    ok = require_startup_position_ack(
                        cfg, logger, ledger, conn=None,
                    )
            self.assertTrue(ok)
            self.assertFalse(os.path.isfile(ext_path))


if __name__ == '__main__':
    unittest.main()
