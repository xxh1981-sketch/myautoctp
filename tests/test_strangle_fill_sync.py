"""宽跨成交入账 / 回放单元测试"""

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ctp_bootstrap  # noqa: F401

from pairtrade.constants import DIRECTION_BUY, OFFSET_OPEN
from spread_ledger import SpreadLegStore
from strangle_fill_sync import (
    apply_strangle_trade_record,
    sync_csv_from_strangle_trades,
    wire_strangle_trade_runtime,
)


def _cfg(tmp, journal_name='journal.jsonl'):
    csv_path = os.path.join(tmp, 'pos.csv')
    journal = os.path.join(tmp, journal_name)
    return {
        'strangle': {'order_ref_min': 500000},
        'dual_strategy': {
            'spread_order_ref_max': 499999,
            'strangle_positions_csv': csv_path,
            'strangle_trade_journal': journal,
            'journal_daily_shards': False,
            'trade_replay_lookback_days': 0,
            'strangle_store_unavailable_fallback': 'allow',
        },
    }


class TestStrangleFillSync(unittest.TestCase):

    def test_spread_trade_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            ledger = MagicMock()
            ok = apply_strangle_trade_record(cfg, ledger, {
                'order_ref': 100,
                'instrument': 'SA609C1000',
                'direction': '0',
                'offset': '0',
                'volume': 1,
                'trade_id': 'T1',
            })
            self.assertFalse(ok)
            ledger.set_leg_claims.assert_not_called()

    def test_strangle_trade_updates_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            ledger = MagicMock()
            trade = {
                'order_ref': 500001,
                'instrument': 'SA609C1000',
                'direction': DIRECTION_BUY,
                'offset': OFFSET_OPEN,
                'volume': 2,
                'trade_id': 'T2',
            }
            self.assertTrue(apply_strangle_trade_record(cfg, ledger, trade))
            self.assertTrue(apply_strangle_trade_record(cfg, ledger, trade) is False)
            ledger.set_leg_claims.assert_called_once()

    def test_sync_from_query_replays_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            ledger = MagicMock()
            conn = MagicMock()
            conn.query_trades_sync.return_value = [
                {
                    'order_ref': 500010,
                    'instrument': 'SA609P900',
                    'direction': '0',
                    'offset': '0',
                    'volume': 1,
                    'price': 50.0,
                    'trade_id': 'Q1',
                    'trade_date': '20260520',
                    'trade_time': '10:00:00',
                },
                {
                    'order_ref': 50,
                    'instrument': 'SA609C1000',
                    'direction': '0',
                    'offset': '0',
                    'volume': 99,
                    'trade_id': 'Q2',
                },
            ]
            n = sync_csv_from_strangle_trades(conn, ledger, cfg, logger=None)
            self.assertEqual(n, 1)
            journal = cfg['dual_strategy']['strangle_trade_journal']
            lines = open(journal, encoding='utf-8').read().strip().splitlines()
            applied = [
                json.loads(line) for line in lines
                if json.loads(line).get('journal_state') == 'applied'
            ]
            self.assertEqual(len(applied), 1)
            self.assertEqual(applied[0]['order_ref'], 500010)

    def test_skip_when_not_in_strangle_tradeinfo(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            cfg['strangle_tradeinfo'] = [{'future': 'SA', 'month': '609'}]
            ledger = MagicMock()
            conn = MagicMock()
            conn._normalize_month = MagicMock(return_value='609')
            conn._runtime_state = {}
            trade = {
                'order_ref': 500001,
                'instrument': 'MA609C1000',
                'direction': DIRECTION_BUY,
                'offset': OFFSET_OPEN,
                'volume': 1,
                'trade_id': 'T3',
            }
            self.assertFalse(
                apply_strangle_trade_record(cfg, ledger, trade, conn=conn),
            )
            ledger.set_leg_claims.assert_not_called()
            journal = cfg['dual_strategy']['strangle_trade_journal']
            body = open(journal, encoding='utf-8').read()
            self.assertIn('not_in_strangle_tradeinfo', body)
            from fill_ledger import pop_fill_csv_status
            applied, reason = pop_fill_csv_status(conn, trade)
            self.assertFalse(applied)
            self.assertEqual(reason, 'not_in_strangle_tradeinfo')

    def test_stash_applied_on_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            cfg['strangle_tradeinfo'] = [{'future': 'SA', 'month': '609'}]
            ledger = MagicMock()
            conn = MagicMock()
            conn._normalize_month = MagicMock(return_value='609')
            conn._runtime_state = {}
            trade = {
                'order_ref': 500001,
                'instrument': 'SA609C1000',
                'direction': DIRECTION_BUY,
                'offset': OFFSET_OPEN,
                'volume': 1,
                'trade_id': 'T4',
            }
            self.assertTrue(
                apply_strangle_trade_record(cfg, ledger, trade, conn=conn),
            )
            from fill_ledger import pop_fill_csv_status
            applied, reason = pop_fill_csv_status(conn, trade)
            self.assertTrue(applied)
            self.assertEqual(reason, '')

    def test_store_unavailable_default_is_skip_fill(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            cfg['dual_strategy'].pop('strangle_store_unavailable_fallback', None)
            ledger = MagicMock()
            conn = MagicMock()
            conn._runtime_state = {}
            trade = {
                'order_ref': 500001,
                'instrument': 'SA609C1000',
                'direction': DIRECTION_BUY,
                'offset': OFFSET_OPEN,
                'volume': 1,
                'trade_id': 'T5b',
            }
            self.assertFalse(apply_strangle_trade_record(cfg, ledger, trade, conn=conn))
            ledger.set_leg_claims.assert_not_called()

    def test_store_unavailable_allow_keeps_legacy_behavior_and_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            cfg['dual_strategy']['strangle_store_unavailable_fallback'] = 'allow'
            ledger = MagicMock()
            conn = MagicMock()
            conn._runtime_state = {}
            trade = {
                'order_ref': 500001,
                'instrument': 'SA609C1000',
                'direction': DIRECTION_BUY,
                'offset': OFFSET_OPEN,
                'volume': 1,
                'trade_id': 'T5',
            }
            logger = MagicMock()
            self.assertTrue(apply_strangle_trade_record(cfg, ledger, trade, logger=logger, conn=conn))
            logger.warning.assert_called()
            self.assertIn('SpreadLegStore 不可用', logger.warning.call_args.args[0])

    def test_store_unavailable_skip_fill_is_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            cfg['dual_strategy']['strangle_store_unavailable_fallback'] = 'skip_fill'
            ledger = MagicMock()
            conn = MagicMock()
            conn._runtime_state = {}
            trade = {
                'order_ref': 500001,
                'instrument': 'SA609C1000',
                'direction': DIRECTION_BUY,
                'offset': OFFSET_OPEN,
                'volume': 1,
                'trade_id': 'T6',
            }
            self.assertFalse(apply_strangle_trade_record(cfg, ledger, trade, conn=conn))
            ledger.set_leg_claims.assert_not_called()
            journal = cfg['dual_strategy']['strangle_trade_journal']
            body = open(journal, encoding='utf-8').read()
            self.assertIn('spread_store_unavailable', body)
            from fill_ledger import pop_fill_csv_status
            applied, reason = pop_fill_csv_status(conn, trade)
            self.assertFalse(applied)
            self.assertEqual(reason, 'spread_store_unavailable')

    def test_spread_owned_skip_reason_is_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            ledger = MagicMock()
            spread_store = SpreadLegStore()
            spread_store.set_leg_claims({'SA609C1000': 1})
            conn = MagicMock()
            conn._runtime_state = {'_spread_leg_store': spread_store}
            trade = {
                'order_ref': 500001,
                'instrument': 'SA609C1000',
                'direction': DIRECTION_BUY,
                'offset': OFFSET_OPEN,
                'volume': 1,
                'trade_id': 'T7',
            }
            self.assertFalse(apply_strangle_trade_record(cfg, ledger, trade, conn=conn))
            ledger.set_leg_claims.assert_not_called()
            journal = cfg['dual_strategy']['strangle_trade_journal']
            body = open(journal, encoding='utf-8').read()
            self.assertIn('"skipped": "spread_owned_only"', body)
            from fill_ledger import pop_fill_csv_status
            applied, reason = pop_fill_csv_status(conn, trade)
            self.assertFalse(applied)
            self.assertEqual(reason, 'spread_owned_only')

    def test_spread_owned_bad_claim_volume_treated_as_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            ledger = MagicMock()
            spread_store = SpreadLegStore()
            spread_store.list_leg_claims = lambda: {'SA609C1000': 'abc'}
            conn = MagicMock()
            conn._runtime_state = {'_spread_leg_store': spread_store}
            trade = {
                'order_ref': 500001,
                'instrument': 'SA609C1000',
                'direction': DIRECTION_BUY,
                'offset': OFFSET_OPEN,
                'volume': 1,
                'trade_id': 'T8',
            }
            logger = MagicMock()
            self.assertTrue(
                apply_strangle_trade_record(
                    cfg, ledger, trade, logger=logger, conn=conn,
                ),
            )
            logger.warning.assert_called()
            self.assertIn('claim', logger.warning.call_args.args[0])
            self.assertIn('无效', logger.warning.call_args.args[0])


class TestWireStrangleTradeRuntime(unittest.TestCase):
    """wire_strangle_trade_runtime must chain any pre-existing handler so a
    later wire ordering change (or a third party hooking in) does not silently
    drop spread/fill_ledger callbacks."""

    def _stub_p_trade(self, order_ref='100'):
        p = MagicMock()
        p.OrderRef = order_ref
        p.InstrumentID = b'SA609C1000'
        p.Direction = b'0'
        p.OffsetFlag = b'0'
        p.Volume = 1
        p.Price = 50.0
        p.TradeID = b''
        p.TradeDate = b''
        p.TradeTime = b''
        return p

    def test_chains_existing_handler(self):
        conn = MagicMock()
        conn._runtime_state = {}
        conn.config = {}

        called = []

        def prev(c, p, l):
            called.append('prev')

        conn._runtime_state['_unified_trade_handler'] = prev
        conn._runtime_state['_strangle_trade_handler'] = prev

        wire_strangle_trade_runtime(conn, MagicMock())

        handler = conn._runtime_state['_strangle_trade_handler']
        handler(conn, self._stub_p_trade(), None)

        self.assertEqual(called, ['prev'])

    def test_no_prev_handler_still_works(self):
        conn = MagicMock()
        conn._runtime_state = {}
        conn.config = {}
        wire_strangle_trade_runtime(conn, MagicMock())
        handler = conn._runtime_state['_strangle_trade_handler']
        handler(conn, self._stub_p_trade(), None)


if __name__ == '__main__':
    unittest.main()
