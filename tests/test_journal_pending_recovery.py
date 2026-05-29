"""journal_pending_recovery 单元测试（崩溃后 pending 自愈，不 import autotrade）。"""

import os
import sys
import tempfile
import types
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if 'pairtrade.constants' not in sys.modules:
    _pt = types.ModuleType('pairtrade')
    _pt_const = types.ModuleType('pairtrade.constants')
    _pt_const.DIRECTION_BUY = '0'
    _pt_const.DIRECTION_SELL = '1'
    _pt_const.OFFSET_OPEN = '0'
    _pt_const.OFFSET_CLOSE = '1'
    _pt.constants = _pt_const
    sys.modules['pairtrade'] = _pt
    sys.modules['pairtrade.constants'] = _pt_const

from trade_journal import (
    append_journal,
    scan_unresolved_pending,
    scan_unresolved_pending_rows,
)
from import_spread_positions import (
    load_spread_positions_csv,
    save_spread_positions_csv,
)
from import_strangle_positions import load_positions_csv, save_positions_csv
from journal_pending_recovery import (
    recover_fill_ledger_pending,
    recover_spread_pending,
    recover_strangle_pending,
)


def _spread_cfg(tmp):
    return {
        'dual_strategy': {
            'journal_daily_shards': False,
            'spread_positions_csv': os.path.join(tmp, 'spread.csv'),
            'spread_trade_journal': os.path.join(tmp, 'spread_journal.jsonl'),
        },
    }


def _strangle_cfg(tmp):
    return {
        'dual_strategy': {
            'journal_daily_shards': False,
            'strangle_positions_csv': os.path.join(tmp, 'strangle.csv'),
            'strangle_trade_journal': os.path.join(tmp, 'strangle_journal.jsonl'),
        },
    }


def _fill_cfg(tmp):
    return {
        'dual_strategy': {
            'journal_daily_shards': False,
            'fill_ledger_csv': os.path.join(tmp, 'fill_ledger.csv'),
            'fill_ledger_journal': os.path.join(tmp, 'fill_journal.jsonl'),
        },
    }


def _pending(journal, cfg, **kw):
    row = {'journal_state': 'pending'}
    row.update(kw)
    append_journal(journal, row, cfg)


class TestScanUnresolvedPendingRows(unittest.TestCase):

    def test_returns_unresolved_rows_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = {'dual_strategy': {'journal_daily_shards': False}}
            j = os.path.join(tmp, 'j.jsonl')
            _pending(j, cfg, dedupe_key='k1', instrument='A', pre_volume=0, post_volume=2)
            _pending(j, cfg, dedupe_key='k2', instrument='B', pre_volume=0, post_volume=1)
            append_journal(j, {'dedupe_key': 'k2', 'journal_state': 'applied'}, cfg)
            rows = scan_unresolved_pending_rows(j, cfg)
            self.assertEqual([r['dedupe_key'] for r in rows], ['k1'])
            self.assertEqual(rows[0]['post_volume'], 2)


class TestRecoverSpreadPending(unittest.TestCase):

    def _journal(self, cfg):
        return cfg['dual_strategy']['spread_trade_journal']

    def test_apply_when_cur_equals_pre(self):
        """CSV 未体现本笔（cur==pre）→ 写到 post 并补 applied，halt 清除。"""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _spread_cfg(tmp)
            path = cfg['dual_strategy']['spread_positions_csv']
            save_spread_positions_csv(path, {})  # cur=0=pre
            _pending(
                self._journal(cfg), cfg,
                dedupe_key='k1', instrument='SA609C1000',
                pre_volume=0, post_volume=2,
            )
            store = MagicMock()
            rec = recover_spread_pending(cfg, store=store, logger=MagicMock())
            self.assertEqual(rec['healed'], 1)
            self.assertEqual(load_spread_positions_csv(path), {'SA609C1000': 2})
            self.assertEqual(
                scan_unresolved_pending(self._journal(cfg), cfg)['unresolved_pending'], 0,
            )
            store.set_leg_claims.assert_called()

    def test_already_when_cur_equals_post(self):
        """CSV 已体现本笔（cur==post）→ 只补 applied，不改 CSV。"""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _spread_cfg(tmp)
            path = cfg['dual_strategy']['spread_positions_csv']
            save_spread_positions_csv(path, {'SA609C1000': 2})  # cur=2=post
            _pending(
                self._journal(cfg), cfg,
                dedupe_key='k1', instrument='SA609C1000',
                pre_volume=0, post_volume=2,
            )
            rec = recover_spread_pending(cfg, logger=MagicMock())
            self.assertEqual(rec['healed'], 1)
            self.assertEqual(load_spread_positions_csv(path), {'SA609C1000': 2})
            self.assertEqual(
                scan_unresolved_pending(self._journal(cfg), cfg)['unresolved_pending'], 0,
            )

    def test_ambiguous_cur_neither_pre_nor_post(self):
        """cur 既非 pre 也非 post → 歧义跳过，保持 halt（不动账本）。"""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _spread_cfg(tmp)
            path = cfg['dual_strategy']['spread_positions_csv']
            save_spread_positions_csv(path, {'SA609C1000': 5})  # cur=5
            _pending(
                self._journal(cfg), cfg,
                dedupe_key='k1', instrument='SA609C1000',
                pre_volume=0, post_volume=2,
            )
            rec = recover_spread_pending(cfg, logger=MagicMock())
            self.assertEqual(rec['healed'], 0)
            self.assertEqual(rec['ambiguous'], 1)
            self.assertEqual(load_spread_positions_csv(path), {'SA609C1000': 5})
            self.assertEqual(
                scan_unresolved_pending(self._journal(cfg), cfg)['unresolved_pending'], 1,
            )

    def test_multiple_pending_same_instrument_is_ambiguous(self):
        """同合约 >1 条 unresolved pending → 全部歧义跳过。"""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _spread_cfg(tmp)
            path = cfg['dual_strategy']['spread_positions_csv']
            save_spread_positions_csv(path, {})
            _pending(
                self._journal(cfg), cfg,
                dedupe_key='k1', instrument='SA609C1000', pre_volume=0, post_volume=2,
            )
            _pending(
                self._journal(cfg), cfg,
                dedupe_key='k2', instrument='SA609C1000', pre_volume=2, post_volume=4,
            )
            rec = recover_spread_pending(cfg, logger=MagicMock())
            self.assertEqual(rec['healed'], 0)
            self.assertEqual(rec['ambiguous'], 2)
            self.assertEqual(load_spread_positions_csv(path), {})

    def test_legacy_pending_without_prepost_is_ambiguous(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _spread_cfg(tmp)
            save_spread_positions_csv(cfg['dual_strategy']['spread_positions_csv'], {})
            _pending(self._journal(cfg), cfg, dedupe_key='k1', instrument='SA609C1000')
            rec = recover_spread_pending(cfg, logger=MagicMock())
            self.assertEqual(rec['healed'], 0)
            self.assertEqual(rec['ambiguous'], 1)

    def test_idempotent_second_run_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _spread_cfg(tmp)
            path = cfg['dual_strategy']['spread_positions_csv']
            save_spread_positions_csv(path, {})
            _pending(
                self._journal(cfg), cfg,
                dedupe_key='k1', instrument='SA609C1000', pre_volume=0, post_volume=2,
            )
            recover_spread_pending(cfg, logger=MagicMock())
            rec2 = recover_spread_pending(cfg, logger=MagicMock())
            self.assertEqual(rec2['healed'], 0)
            self.assertEqual(rec2['ambiguous'], 0)
            self.assertEqual(load_spread_positions_csv(path), {'SA609C1000': 2})

    def test_apply_removes_when_post_zero(self):
        """平掉到 0（post==0）→ 自愈后从 CSV 移除该合约。"""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _spread_cfg(tmp)
            path = cfg['dual_strategy']['spread_positions_csv']
            save_spread_positions_csv(path, {'SA609C1000': 2})  # cur=2=pre
            _pending(
                self._journal(cfg), cfg,
                dedupe_key='k1', instrument='SA609C1000',
                pre_volume=2, post_volume=0,
            )
            rec = recover_spread_pending(cfg, logger=MagicMock())
            self.assertEqual(rec['healed'], 1)
            self.assertEqual(load_spread_positions_csv(path), {})


class TestRecoverStranglePending(unittest.TestCase):

    def _journal(self, cfg):
        return cfg['dual_strategy']['strangle_trade_journal']

    def test_apply_when_cur_equals_pre(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _strangle_cfg(tmp)
            path = cfg['dual_strategy']['strangle_positions_csv']
            save_positions_csv(path, {'SA609P900': 1})  # cur=1=pre
            _pending(
                self._journal(cfg), cfg,
                dedupe_key='k1', instrument='SA609P900', pre_volume=1, post_volume=3,
            )
            ledger = MagicMock()
            rec = recover_strangle_pending(cfg, ledger=ledger, logger=MagicMock())
            self.assertEqual(rec['healed'], 1)
            self.assertEqual(load_positions_csv(path), {'SA609P900': 3})
            ledger.set_leg_claims.assert_called()

    def test_apply_removes_when_post_non_positive(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _strangle_cfg(tmp)
            path = cfg['dual_strategy']['strangle_positions_csv']
            save_positions_csv(path, {'SA609P900': 2})  # cur=2=pre
            _pending(
                self._journal(cfg), cfg,
                dedupe_key='k1', instrument='SA609P900', pre_volume=2, post_volume=0,
            )
            rec = recover_strangle_pending(cfg, logger=MagicMock())
            self.assertEqual(rec['healed'], 1)
            self.assertEqual(load_positions_csv(path), {})


class TestRecoverFillLedgerPending(unittest.TestCase):

    def test_marks_applied_without_reappend(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _fill_cfg(tmp)
            journal = cfg['dual_strategy']['fill_ledger_journal']
            csv_path = cfg['dual_strategy']['fill_ledger_csv']
            _pending(journal, cfg, dedupe_key='k1', instrument='SA609C1000')
            rec = recover_fill_ledger_pending(cfg, logger=MagicMock())
            self.assertEqual(rec['healed'], 1)
            self.assertEqual(
                scan_unresolved_pending(journal, cfg)['unresolved_pending'], 0,
            )
            # 未重复 append 分析行：CSV 不存在（本测试未建过）或保持空。
            self.assertFalse(os.path.isfile(csv_path))


if __name__ == '__main__':
    unittest.main()
