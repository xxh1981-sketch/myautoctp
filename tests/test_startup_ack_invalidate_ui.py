"""启动确认窗「清除旧确认并刷新」与 _detect_stale_ack 单测。"""

import os
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ctp_bootstrap  # noqa: F401

from merged_startup_ack import (
    _INVALIDATE_LABEL,
    _detect_stale_ack,
    _format_stale_ack_warning,
    _prompt_ledger_reconcile_step,
    _run_invalidate_ack,
    require_startup_position_ack,
)
from startup_ack_fingerprint import save_startup_ack_fingerprint


def _ledger_cfg(tmp: str) -> dict:
    spread = os.path.join(tmp, 'spread_positions.csv')
    strangle = os.path.join(tmp, 'strangle_positions.csv')
    ledger = os.path.join(tmp, 'ledger_strangle.json')
    ack = os.path.join(tmp, 'position_startup_ack.txt')
    for path in (spread, strangle, ledger):
        with open(path, 'w', encoding='utf-8') as f:
            f.write('1\n')
    with open(ack, 'w', encoding='utf-8') as f:
        f.write('confirmed\n')
    return {
        'dual_strategy': {
            'startup_ack_file': ack,
            'spread_positions_csv': spread,
            'strangle_positions_csv': strangle,
            'startup_ack_track_ledger_files': True,
        },
        'strangle': {'ledger_path': ledger},
    }


def _mock_ledger():
    ledger = MagicMock()
    ledger.list_positions.return_value = []
    ledger.list_leg_claims.return_value = {}
    ledger.list_unmatched_legs.return_value = []
    ledger.get_daily_buy_amount.return_value = 0.0
    return ledger


class TestDetectStaleAck(unittest.TestCase):

    def test_no_ack_file_not_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _ledger_cfg(tmp)
            os.remove(cfg['dual_strategy']['startup_ack_file'])
            stale, reasons = _detect_stale_ack(cfg)
            self.assertFalse(stale)
            self.assertEqual(reasons, [])

    def test_ack_without_meta_not_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _ledger_cfg(tmp)
            stale, reasons = _detect_stale_ack(cfg)
            self.assertFalse(stale)
            self.assertEqual(reasons, [])

    def test_matching_fingerprint_not_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _ledger_cfg(tmp)
            save_startup_ack_fingerprint(cfg)
            stale, reasons = _detect_stale_ack(cfg)
            self.assertFalse(stale)
            self.assertEqual(reasons, [])

    def test_modified_csv_is_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _ledger_cfg(tmp)
            save_startup_ack_fingerprint(cfg)
            spread = cfg['dual_strategy']['spread_positions_csv']
            time.sleep(0.05)
            with open(spread, 'a', encoding='utf-8') as f:
                f.write('2\n')
            stale, reasons = _detect_stale_ack(cfg)
            self.assertTrue(stale)
            self.assertTrue(any('spread_positions' in r for r in reasons))

    def test_track_disabled_not_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _ledger_cfg(tmp)
            save_startup_ack_fingerprint(cfg)
            cfg['dual_strategy']['startup_ack_track_ledger_files'] = False
            spread = cfg['dual_strategy']['spread_positions_csv']
            with open(spread, 'a', encoding='utf-8') as f:
                f.write('x\n')
            stale, _ = _detect_stale_ack(cfg)
            self.assertFalse(stale)


class TestStaleAckHelpers(unittest.TestCase):

    def test_format_warning_includes_label(self):
        text = _format_stale_ack_warning(['spread_positions.csv: 文件已在确认后修改'])
        self.assertIn('旧确认已失配', text)
        self.assertIn(_INVALIDATE_LABEL, text)

    def test_format_warning_empty_reasons(self):
        self.assertEqual(_format_stale_ack_warning([]), '')

    def test_run_invalidate_removes_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _ledger_cfg(tmp)
            ext = os.path.join(tmp, 'external_positions_ack.json')
            cfg['dual_strategy']['external_positions_ack_file'] = ext
            with open(ext, 'w', encoding='utf-8') as f:
                f.write('{}')
            save_startup_ack_fingerprint(cfg)
            logger = MagicMock()
            removed = _run_invalidate_ack(cfg, logger)
            ack = cfg['dual_strategy']['startup_ack_file']
            self.assertIn(ack, removed)
            self.assertFalse(os.path.isfile(ack))
            logger.warning.assert_called()


class TestPromptLedgerReconcileInvalidate(unittest.TestCase):

    @patch('merged_startup_ack._prompt_reconcile_mismatch_ack', return_value='yes')
    def test_stale_passes_allow_invalidate_and_header(self, mock_prompt):
        reasons = ['spread_positions.csv: 文件已在确认后修改']
        _prompt_ledger_reconcile_step(
            {}, MagicMock(), 'summary', [], False,
            allow_invalidate=True, stale_reasons=reasons,
        )
        _, kwargs = mock_prompt.call_args
        self.assertTrue(kwargs.get('allow_invalidate'))
        header = kwargs.get('header') or ''
        self.assertIn('旧确认已失配', header)
        self.assertIn(_INVALIDATE_LABEL, header)


class TestRequireStartupAckInvalidateLoop(unittest.TestCase):

    def _flow_config(self, **dual_overrides):
        dual = {
            'require_startup_ack': True,
            'startup_ack_each_run': True,
            'startup_ack_interactive': True,
            'startup_ack_use_gui': False,
        }
        dual.update(dual_overrides)
        return {
            'dual_strategy': dual,
            'strangle_tradeinfo': [],
            'spread_tradeinfo': [],
        }

    @patch('merged_startup_ack._run_account_decomposition_step', return_value=True)
    @patch(
        'merged_startup_ack._prompt_ledger_reconcile_step',
        side_effect=['invalidate', 'yes'],
    )
    @patch(
        'merged_startup_ack._detect_stale_ack',
        return_value=(True, ['spread_positions.csv: 文件已在确认后修改']),
    )
    @patch('merged_startup_ack._preview_reconcile', return_value=(False, [], []))
    @patch('merged_startup_ack._format_ctp_positions_preview', return_value='')
    def test_invalidate_loops_then_proceeds(
        self, _ctp, _preview, _stale, ledger_step, _decomp,
    ):
        ok = require_startup_position_ack(
            self._flow_config(), MagicMock(), _mock_ledger(),
            MagicMock(_runtime_state={}),
        )
        self.assertTrue(ok)
        self.assertEqual(ledger_step.call_count, 2)
        first_kwargs = ledger_step.call_args_list[0].kwargs
        self.assertTrue(first_kwargs.get('allow_invalidate'))
        _decomp.assert_called_once()

    @patch('merged_startup_ack._run_account_decomposition_step', return_value=True)
    @patch('merged_startup_ack._prompt_ledger_reconcile_step', return_value='yes')
    @patch('merged_startup_ack._preview_reconcile', return_value=(False, [], []))
    @patch('merged_startup_ack._format_ctp_positions_preview', return_value='')
    def test_no_stale_skips_allow_invalidate(
        self, _ctp, _preview, ledger_step, _decomp,
    ):
        with patch('merged_startup_ack._detect_stale_ack', return_value=(False, [])):
            ok = require_startup_position_ack(
                self._flow_config(), MagicMock(), _mock_ledger(),
                MagicMock(_runtime_state={}),
            )
        self.assertTrue(ok)
        kwargs = ledger_step.call_args.kwargs
        self.assertFalse(kwargs.get('allow_invalidate'))

    @patch('merged_startup_ack._run_account_decomposition_step', return_value=True)
    @patch(
        'merged_startup_ack._prompt_ledger_reconcile_step',
        side_effect=['invalidate', 'yes'],
    )
    @patch('merged_startup_ack._preview_reconcile', return_value=(False, [], []))
    @patch('merged_startup_ack._format_ctp_positions_preview', return_value='')
    def test_stale_detected_from_real_fingerprint(
        self, _ctp, _preview, ledger_step, _decomp,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _ledger_cfg(tmp)
            save_startup_ack_fingerprint(cfg)
            spread = cfg['dual_strategy']['spread_positions_csv']
            time.sleep(0.05)
            with open(spread, 'a', encoding='utf-8') as f:
                f.write('changed\n')
            flow = self._flow_config()
            cfg['dual_strategy'].update(flow['dual_strategy'])
            cfg['strangle_tradeinfo'] = flow['strangle_tradeinfo']
            cfg['spread_tradeinfo'] = flow['spread_tradeinfo']
            cfg['_manual_start'] = True
            logger = MagicMock()
            ok = require_startup_position_ack(
                cfg, logger, _mock_ledger(),
                MagicMock(_runtime_state={}),
            )
        self.assertTrue(ok)
        self.assertEqual(ledger_step.call_count, 2)
        self.assertTrue(ledger_step.call_args_list[0].kwargs.get('allow_invalidate'))
        logged = ' '.join(str(c) for c in logger.warning.call_args_list)
        self.assertIn('旧启动确认', logged)


if __name__ == '__main__':
    unittest.main()
