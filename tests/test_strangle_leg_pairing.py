"""strangle_leg_pairing unit tests."""

import os
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ctp_bootstrap  # noqa: F401

from strangle_leg_pairing import (
    INFERRED_COMPLETE_KIND,
    INFERRED_FLAG,
    INFERRED_SINGLE_KIND,
    _build_inferred_complete_item,
    _canonical_instrument,
    _prune_inferred_open_queues,
    executor_close_pending_instruments,
    get_install_error,
    has_executor_open_phase2_pending,
    infer_groups_from_remaining_claims,
    install_strangle_leg_pairing_patch,
    is_close_unmatched_item,
    pair_calls_puts_descending,
    should_suppress_inferred_open_rebalance,
    single_leg_should_close,
    subtract_ledger_coverage,
    sync_inferred_strangle_positions,
)
import strangle_leg_pairing as leg_pairing_mod


class _Conn:
    def __init__(self, future_price=2400.0, dte=90, atm_call=None):
        self.future_prices = {'rm': future_price, 'ma': future_price}
        self._dte = dte
        self.options_by_strike = {}
        self.option_quotes = {}
        if atm_call is not None:
            # atm_call: (strike, bid, ask) 提供一个平值 Call 报价供溢价率计算
            strike, bid, ask = atm_call
            norm = self._normalize_month('rm', '2609')
            inst = f'RM{norm}C{int(strike)}'
            self.options_by_strike = {'rm': {norm: {float(strike): {'call': inst}}}}
            self.option_quotes = {inst: {'bid': bid, 'ask': ask}}

    def _normalize_month(self, symbol, month):
        m = str(month)
        if len(m) == 4 and m.isdigit():
            return m[1:]
        return m

    def get_days_to_expiry(self, sym, month):
        return self._dte


# HV 关闭（指向不存在文件 → sigma 回退 vol_basis），单测确定性
_NO_HV = {'strangle': {'hv_close_path': '/nonexistent/futures.xlsx'}}


class PairCallsPutsDescendingTests(unittest.TestCase):
    def test_rm_style_pairing_leaves_single_call(self):
        calls = [
            {'inst': 'RM609C2750', 'strike': 2750.0, 'vol': 47},
            {'inst': 'RM609C2650', 'strike': 2650.0, 'vol': 2},
            {'inst': 'RM609C2600', 'strike': 2600.0, 'vol': 2},
        ]
        puts = [
            {'inst': 'RM609P2075', 'strike': 2075.0, 'vol': 1},
            {'inst': 'RM609P2050', 'strike': 2050.0, 'vol': 2},
            {'inst': 'RM609P2025', 'strike': 2025.0, 'vol': 47},
        ]
        pairs, singles = pair_calls_puts_descending(calls, puts)
        total_groups = sum(p['groups'] for p in pairs)
        self.assertEqual(total_groups, 50)
        self.assertEqual(len(singles), 1)
        self.assertEqual(singles[0]['inst'], 'RM609C2600')
        self.assertEqual(singles[0]['vol'], 1)
        self.assertEqual(pairs[0]['call_strike'], 2750.0)
        self.assertEqual(pairs[0]['put_strike'], 2075.0)
        self.assertEqual(pairs[0]['groups'], 1)


class SyncInferredTests(unittest.TestCase):
    def test_sync_creates_inferred_groups_and_single_close(self):
        from straggle_ledger import StrangleLedger

        conn = _Conn()
        with tempfile.TemporaryDirectory() as td:
            ledger = StrangleLedger(os.path.join(td, 'l.json'))
            ledger.set_leg_claims({
                'RM609C2750': 47,
                'RM609C2650': 2,
                'RM609C2600': 2,
                'RM609P2075': 1,
                'RM609P2050': 2,
                'RM609P2025': 47,
            })
            stats = sync_inferred_strangle_positions(
                ledger, 'rm', '2609', conn, 0.01, logger=None,
                vix_engine=None, config=_NO_HV,
                tradeinfo_item={
                    'future': 'rm', 'month': '2609', 'vol_basis': 0.01,
                    'vol_of_combo': 50, 'min_tick': 0.5,
                },
            )
            self.assertGreater(stats['pairs'], 0)
            self.assertEqual(stats['singles'], 1)
            singles = [
                u for u in ledger.list_unmatched_legs('rm', '2609')
                if u.get('kind') == INFERRED_SINGLE_KIND
            ]
            self.assertEqual(len(singles), 1)
            self.assertEqual(singles[0].get('stage'), 'close')
            self.assertTrue(is_close_unmatched_item(singles[0]))

    @unittest.mock.patch('strangle_leg_pairing._try_entry_for_complete')
    def test_cooldown_blocks_complete_buy_and_falls_back_to_close(self, mock_entry):
        """平仓冷却期内不得靠补腿重新开仓（check_entry 不管冷却）。"""
        from straggle_ledger import StrangleLedger

        mock_entry.return_value = (True, {
            'call_inst': 'RM609C2600',
            'put_inst': 'RM609P2025',
            'call_strike': 2600.0,
            'put_strike': 2025.0,
        })
        conn = _Conn()
        with tempfile.TemporaryDirectory() as td:
            ledger = StrangleLedger(os.path.join(td, 'l.json'))
            ledger.set_leg_claims({'RM609C2600': 1})
            ledger.add_cooldown('rm', '2609', 300)
            sync_inferred_strangle_positions(
                ledger, 'rm', '2609', conn, 0.01, logger=None,
                vix_engine=None, config=_NO_HV,
                tradeinfo_item={
                    'future': 'rm', 'month': '2609', 'vol_basis': 0.01,
                    'vol_of_combo': 50, 'min_tick': 0.5,
                },
            )
            mock_entry.assert_not_called()
            rows = ledger.list_unmatched_legs('rm', '2609')
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['kind'], INFERRED_SINGLE_KIND)
            self.assertEqual(rows[0]['stage'], 'close')
            self.assertEqual(rows[0]['action'], 'SELL')

    @unittest.mock.patch('strangle_leg_pairing._try_entry_for_complete')
    def test_open_halt_blocks_complete_buy(self, mock_entry):
        from straggle_ledger import StrangleLedger

        mock_entry.return_value = (True, {
            'call_inst': 'RM609C2600',
            'put_inst': 'RM609P2025',
            'call_strike': 2600.0,
            'put_strike': 2025.0,
        })
        conn = _Conn()
        with tempfile.TemporaryDirectory() as td:
            ledger = StrangleLedger(os.path.join(td, 'l.json'))
            ledger.set_leg_claims({'RM609C2600': 1})
            ledger.set_open_halt(True, '对账不一致')
            sync_inferred_strangle_positions(
                ledger, 'rm', '2609', conn, 0.01, logger=None,
                vix_engine=None, config=_NO_HV,
                tradeinfo_item={
                    'future': 'rm', 'month': '2609', 'vol_basis': 0.01,
                    'vol_of_combo': 50, 'min_tick': 0.5,
                },
            )
            mock_entry.assert_not_called()
            rows = ledger.list_unmatched_legs('rm', '2609')
            self.assertEqual(rows[0]['stage'], 'close')

    @unittest.mock.patch('strangle_leg_pairing._try_entry_for_complete')
    def test_sync_single_leg_complete_when_entry_ok(self, mock_entry):
        from straggle_ledger import StrangleLedger

        mock_entry.return_value = (True, {
            'call_inst': 'RM609C2600',
            'put_inst': 'RM609P2025',
            'call_strike': 2600.0,
            'put_strike': 2025.0,
        })
        conn = _Conn()
        with tempfile.TemporaryDirectory() as td:
            ledger = StrangleLedger(os.path.join(td, 'l.json'))
            ledger.set_leg_claims({
                'RM609C2600': 2,
                'RM609P2025': 1,
            })
            sync_inferred_strangle_positions(
                ledger, 'rm', '2609', conn, 0.01, logger=None,
                vix_engine=None, config=_NO_HV,
                tradeinfo_item={
                    'future': 'rm', 'month': '2609', 'vol_basis': 0.01,
                    'vol_of_combo': 50, 'min_tick': 0.5,
                },
            )
            complete = [
                u for u in ledger.list_unmatched_legs('rm', '2609')
                if u.get('kind') == INFERRED_COMPLETE_KIND
            ]
            self.assertEqual(len(complete), 1)
            self.assertEqual(complete[0]['stage'], 'open')
            self.assertEqual(complete[0]['action'], 'BUY')
            self.assertEqual(complete[0]['leg']['inst'], 'RM609P2025')
            self.assertFalse(is_close_unmatched_item(complete[0]))

    @unittest.mock.patch('strangle_leg_pairing._try_entry_for_complete')
    def test_sync_skips_inferred_complete_when_awaiting_phase2(self, mock_entry):
        """认领层不得与执行器 awaiting_phase2 重复补开仓第二腿。"""
        from straggle_ledger import StrangleLedger

        mock_entry.return_value = (True, {
            'call_inst': 'm2701-C-3300',
            'put_inst': 'm2701-P-2900',
            'call_strike': 3300.0,
            'put_strike': 2900.0,
        })
        conn = _Conn(future_price=3088.0)
        with tempfile.TemporaryDirectory() as td:
            ledger = StrangleLedger(os.path.join(td, 'l.json'))
            ledger.set_leg_claims({'M2701-C-3300': 1})
            ledger.add_awaiting_phase2({
                'symbol': 'm',
                'month': '2701',
                'leg': {'inst': 'm2701-P-2900', 'label': 'put'},
                'filled_instrument': 'm2701-C-3300',
                'volume': 1,
                'target_groups': 1,
                'action': 'BUY',
                'stage': 'open',
                'call_inst': 'm2701-C-3300',
                'put_inst': 'm2701-P-2900',
            })
            sync_inferred_strangle_positions(
                ledger, 'm', '2701', conn, 0.01, logger=None,
                vix_engine=None, config=_NO_HV,
                tradeinfo_item={
                    'future': 'm', 'month': '2701', 'vol_basis': 0.01,
                    'vol_of_combo': 6, 'min_tick': 0.5,
                },
            )
            kinds = [u.get('kind') for u in ledger.list_unmatched_legs('m', '2701')]
            self.assertEqual(kinds, ['awaiting_phase2'])
            self.assertTrue(has_executor_open_phase2_pending(ledger, 'm', '2701'))

    def test_sync_skips_inferred_single_close_when_close_chp_pending(self):
        from straggle_ledger import StrangleLedger

        conn = _Conn(future_price=0.0, dte=90)
        with tempfile.TemporaryDirectory() as td:
            ledger = StrangleLedger(os.path.join(td, 'l.json'))
            ledger.set_leg_claims({'RM609P2025': 1})
            ledger.add_unmatched_leg({
                'kind': 'close_chp_pending',
                'position_id': 'pos-1',
                'symbol': 'rm',
                'month': '2609',
                'leg': {'inst': 'RM609P2025', 'label': 'put'},
                'volume': 1,
                'action': 'SELL',
                'stage': 'close',
            })
            sync_inferred_strangle_positions(
                ledger, 'rm', '2609', conn, 0.01, logger=None,
                vix_engine=None, config=_NO_HV,
                tradeinfo_item={
                    'future': 'rm', 'month': '2609', 'vol_basis': 0.01,
                    'vol_of_combo': 50, 'min_tick': 0.5,
                },
            )
            kinds = [u.get('kind') for u in ledger.list_unmatched_legs('rm', '2609')]
            self.assertEqual(kinds, ['close_chp_pending'])
            self.assertIn(
                'RM609P2025',
                executor_close_pending_instruments(ledger, 'rm', '2609'),
            )

    def test_prune_inferred_open_when_awaiting_phase2_exists(self):
        from straggle_ledger import StrangleLedger

        with tempfile.TemporaryDirectory() as td:
            ledger = StrangleLedger(os.path.join(td, 'l.json'))
            ledger.add_unmatched_leg({
                'kind': INFERRED_COMPLETE_KIND,
                'symbol': 'm',
                'month': '2701',
                'leg': {'inst': 'm2701-P-2900', 'label': 'put'},
                'stage': 'open',
                'action': 'BUY',
                'volume': 1,
            })
            ledger.add_unmatched_leg({
                'kind': 'awaiting_phase2',
                'symbol': 'm',
                'month': '2701',
                'leg': {'inst': 'm2701-P-2900', 'label': 'put'},
                'filled_instrument': 'm2701-C-3300',
                'volume': 1,
                'stage': 'open',
            })
            removed = _prune_inferred_open_queues(ledger, 'm', '2701')
            self.assertEqual(removed, 1)
            kinds = [u.get('kind') for u in ledger.list_unmatched_legs('m', '2701')]
            self.assertEqual(kinds, ['awaiting_phase2'])

    def test_add_awaiting_phase2_patch_prunes_inferred_complete(self):
        from straggle_ledger import StrangleLedger

        install_strangle_leg_pairing_patch()
        with tempfile.TemporaryDirectory() as td:
            ledger = StrangleLedger(os.path.join(td, 'l.json'))
            ledger.add_unmatched_leg({
                'kind': INFERRED_COMPLETE_KIND,
                'symbol': 'm',
                'month': '2701',
                'leg': {'inst': 'm2701-P-2900', 'label': 'put'},
                'stage': 'open',
                'action': 'BUY',
                'volume': 1,
            })
            ledger.add_awaiting_phase2({
                'symbol': 'm',
                'month': '2701',
                'leg': {'inst': 'm2701-P-2900', 'label': 'put'},
                'filled_instrument': 'm2701-C-3300',
                'volume': 1,
                'stage': 'open',
            })
            kinds = [u.get('kind') for u in ledger.list_unmatched_legs('m', '2701')]
            self.assertEqual(kinds, ['awaiting_phase2'])

    def test_should_suppress_inferred_open_rebalance(self):
        from straggle_ledger import StrangleLedger

        with tempfile.TemporaryDirectory() as td:
            ledger = StrangleLedger(os.path.join(td, 'l.json'))
            inferred = {
                'kind': INFERRED_COMPLETE_KIND,
                'symbol': 'm',
                'month': '2701',
                'stage': 'open',
            }
            self.assertFalse(should_suppress_inferred_open_rebalance(ledger, inferred))
            ledger.add_awaiting_phase2({
                'symbol': 'm',
                'month': '2701',
                'leg': {'inst': 'm2701-P-2900', 'label': 'put'},
                'volume': 1,
                'stage': 'open',
            })
            self.assertTrue(should_suppress_inferred_open_rebalance(ledger, inferred))

    def test_subtract_existing_ledger_before_infer(self):
        from straggle_ledger import StrangleLedger

        conn = _Conn()
        with tempfile.TemporaryDirectory() as td:
            ledger = StrangleLedger(os.path.join(td, 'l.json'))
            ledger.set_leg_claims({'MA609C3650': 10, 'MA609P1900': 10})
            pos = ledger.create_position(
                'ma', '609', 'MA609C3650', 'MA609P1900',
                3650.0, 1900.0, 0.01, groups=6,
            )
            remaining = subtract_ledger_coverage(
                {'MA609C3650': 10, 'MA609P1900': 10},
                ledger, 'ma', '609',
            )
            self.assertEqual(remaining.get('MA609C3650'), 4)
            self.assertEqual(remaining.get('MA609P1900'), 4)
            pairs, singles = infer_groups_from_remaining_claims(
                remaining, conn, 'ma', '609',
            )
            self.assertEqual(sum(p['groups'] for p in pairs), 4)
            self.assertEqual(singles, [])

    def test_dce_corn_open_group_not_inferred_single_close(self):
        """Regression: c2701-P-* must not be misclassified; ledger open covers claims."""
        from straggle_ledger import StrangleLedger
        from spread_ledger import SpreadLegStore

        self.assertFalse(SpreadLegStore._is_call_instrument('C2701-P-2120'))
        self.assertTrue(SpreadLegStore._is_call_instrument('C2701-C-2320'))

        conn = _Conn()
        conn.future_prices = {'c': 2300.0}

        class _CConn(_Conn):
            def _normalize_month(self, sym, month):
                m = str(month)
                if len(m) == 4 and m.isdigit():
                    return m[1:]
                return m

        conn = _CConn()
        with tempfile.TemporaryDirectory() as td:
            ledger = StrangleLedger(os.path.join(td, 'l.json'))
            ledger.set_leg_claims({
                'C2701-C-2320': 1,
                'C2701-P-2120': 1,
            })
            ledger.create_position(
                'c', '2701', 'c2701-C-2320', 'c2701-P-2120',
                2320.0, 2120.0, 0.01, groups=1,
            )
            stats = sync_inferred_strangle_positions(
                ledger, 'c', '2701', conn, 0.01, logger=None,
                vix_engine=None, config=_NO_HV,
                tradeinfo_item={
                    'future': 'c', 'month': '2701', 'vol_basis': 0.01,
                    'vol_of_combo': 1, 'min_tick': 0.5,
                },
            )
            self.assertEqual(stats['singles'], 0)
            close_singles = [
                u for u in ledger.list_unmatched_legs('c', '2701')
                if u.get('kind') == INFERRED_SINGLE_KIND and u.get('stage') == 'close'
            ]
            self.assertEqual(close_singles, [])


class BuildCompleteItemTests(unittest.TestCase):
    def test_call_orphan_buys_put(self):
        row = {'inst': 'RM609C2600', 'strike': 2600.0, 'side': 'call', 'vol': 1}
        entry = {
            'call_inst': 'RM609C2750',
            'put_inst': 'RM609P2025',
            'call_strike': 2750.0,
            'put_strike': 2025.0,
        }
        item = _build_inferred_complete_item(
            row, entry, 'rm', '2609', 0.01, 2400.0,
        )
        self.assertEqual(item['leg']['inst'], 'RM609P2025')
        self.assertEqual(item['call_inst'], 'RM609C2600')
        self.assertEqual(item['stage'], 'open')

    def test_canonical_instrument_uses_quotes_key(self):
        class _Conn:
            quotes = {'m2701-P-2900': object()}

        inst = _canonical_instrument(_Conn(), 'M2701-P-2900')
        self.assertEqual(inst, 'm2701-P-2900')


class SingleLegExitTests(unittest.TestCase):
    def test_vol_exit_on_single_leg(self):
        # sigma=vol_basis=0.2; thr=0.40*sqrt(90/365)*0.2=0.0397;
        # ATM Call mid=120 -> rate=120/2400=0.05 > thr -> 波动率平仓
        conn = _Conn(future_price=2400.0, dte=90, atm_call=(2400, 118.0, 122.0))
        row = {'inst': 'RM609C2600', 'strike': 2600.0, 'side': 'call', 'vol': 1}
        ok, reason = single_leg_should_close(
            conn, row, 'rm', '2609', None, _NO_HV, 0.2,
        )
        self.assertTrue(ok)
        self.assertIn('波动率平仓', reason)

    def test_no_vol_exit_when_premium_low(self):
        # ATM Call mid=50 -> rate=0.0208 < thr 0.0397 -> 不平仓
        conn = _Conn(future_price=2400.0, dte=90, atm_call=(2400, 48.0, 52.0))
        row = {'inst': 'RM609C2600', 'strike': 2600.0, 'side': 'call', 'vol': 1}
        ok, _reason = single_leg_should_close(
            conn, row, 'rm', '2609', None, _NO_HV, 0.2,
        )
        self.assertFalse(ok)

    def test_no_exit_when_future_invalid(self):
        conn = _Conn(future_price=0.0, dte=90)
        row = {'inst': 'RM609C2600', 'strike': 2600.0, 'side': 'call', 'vol': 1}
        ok, _reason = single_leg_should_close(
            conn, row, 'rm', '2609', None, _NO_HV, 0.01,
        )
        self.assertFalse(ok)

    def test_sync_single_close_when_future_invalid(self):
        from straggle_ledger import StrangleLedger

        conn = _Conn(future_price=0.0, dte=90)
        with tempfile.TemporaryDirectory() as td:
            ledger = StrangleLedger(os.path.join(td, 'l.json'))
            ledger.set_leg_claims({
                'RM609C2600': 2,
                'RM609P2025': 1,
            })
            sync_inferred_strangle_positions(
                ledger, 'rm', '2609', conn, 0.01, logger=None,
                vix_engine=None, config=_NO_HV,
                tradeinfo_item={
                    'future': 'rm', 'month': '2609', 'vol_basis': 0.01,
                    'vol_of_combo': 50, 'min_tick': 0.5,
                },
            )
            singles = [
                u for u in ledger.list_unmatched_legs('rm', '2609')
                if u.get('kind') == INFERRED_SINGLE_KIND
            ]
            self.assertEqual(len(singles), 1)
            self.assertEqual(singles[0].get('stage'), 'close')
            self.assertEqual(singles[0].get('action'), 'SELL')
            self.assertTrue(is_close_unmatched_item(singles[0]))

    def test_inferred_coverage_survives_second_sync(self):
        """认领被推断仓覆盖后，下一轮 sync 不得删掉推断仓（否则隔轮又开仓）。"""
        from straggle_ledger import StrangleLedger

        conn = _Conn()
        with tempfile.TemporaryDirectory() as td:
            ledger = StrangleLedger(os.path.join(td, 'l.json'))
            ledger.set_leg_claims({
                'C2701-C-2340': 1,
                'C2701-P-2140': 1,
            })
            item = {
                'future': 'c', 'month': '2701', 'vol_basis': 0.01,
                'vol_of_combo': 1, 'min_tick': 0.5,
            }
            sync_inferred_strangle_positions(
                ledger, 'c', '2701', conn, 0.01, logger=None,
                vix_engine=None, config=_NO_HV, tradeinfo_item=item,
            )
            open1 = [
                p for p in ledger.list_positions('c', '2701')
                if p.get('status') == 'open' and p.get(INFERRED_FLAG)
            ]
            self.assertEqual(len(open1), 1)
            self.assertEqual(open1[0]['call_instrument'], 'c2701-C-2340')
            self.assertEqual(open1[0]['put_instrument'], 'c2701-P-2140')
            self.assertEqual(ledger.count_open_groups('c', '2701'), 1)
            pid = open1[0]['id']

            # 第二轮：认领已被该推断仓覆盖 → remaining 空，必须保留而非 prune
            sync_inferred_strangle_positions(
                ledger, 'c', '2701', conn, 0.01, logger=None,
                vix_engine=None, config=_NO_HV, tradeinfo_item=item,
            )
            open2 = [
                p for p in ledger.list_positions('c', '2701')
                if p.get('status') == 'open' and p.get(INFERRED_FLAG)
            ]
            self.assertEqual(len(open2), 1)
            self.assertEqual(open2[0]['id'], pid)
            self.assertEqual(ledger.count_open_groups('c', '2701'), 1)

    def test_empty_claims_prunes_inferred_when_ctp_flat(self):
        from straggle_ledger import StrangleLedger

        conn = _Conn()
        with tempfile.TemporaryDirectory() as td:
            ledger = StrangleLedger(os.path.join(td, 'l.json'))
            ledger.set_leg_claims({
                'C2701-C-2340': 1,
                'C2701-P-2140': 1,
            })
            item = {
                'future': 'c', 'month': '2701', 'vol_basis': 0.01,
                'vol_of_combo': 1, 'min_tick': 0.5,
            }
            sync_inferred_strangle_positions(
                ledger, 'c', '2701', conn, 0.01, logger=None,
                vix_engine=None, config=_NO_HV, tradeinfo_item=item,
            )
            self.assertEqual(ledger.count_open_groups('c', '2701'), 1)
            ledger.set_leg_claims({})
            with unittest.mock.patch(
                'strangle_leg_pairing._symbol_month_has_ctp_long',
                return_value=False,
            ):
                sync_inferred_strangle_positions(
                    ledger, 'c', '2701', conn, 0.01, logger=None,
                    vix_engine=None, config=_NO_HV, tradeinfo_item=item,
                )
            self.assertEqual(ledger.count_open_groups('c', '2701'), 0)

    def test_empty_claims_keeps_inferred_when_ctp_still_long(self):
        from straggle_ledger import StrangleLedger

        conn = _Conn()
        with tempfile.TemporaryDirectory() as td:
            ledger = StrangleLedger(os.path.join(td, 'l.json'))
            ledger.set_leg_claims({
                'C2701-C-2340': 1,
                'C2701-P-2140': 1,
            })
            item = {
                'future': 'c', 'month': '2701', 'vol_basis': 0.01,
                'vol_of_combo': 1, 'min_tick': 0.5,
            }
            sync_inferred_strangle_positions(
                ledger, 'c', '2701', conn, 0.01, logger=None,
                vix_engine=None, config=_NO_HV, tradeinfo_item=item,
            )
            self.assertEqual(ledger.count_open_groups('c', '2701'), 1)
            ledger.set_leg_claims({})
            with unittest.mock.patch(
                'strangle_leg_pairing._symbol_month_has_ctp_long',
                return_value=True,
            ):
                sync_inferred_strangle_positions(
                    ledger, 'c', '2701', conn, 0.01, logger=None,
                    vix_engine=None, config=_NO_HV, tradeinfo_item=item,
                )
            self.assertEqual(ledger.count_open_groups('c', '2701'), 1)

    def test_inferred_open_allowed_fail_closed(self):
        from strangle_leg_pairing import _inferred_open_allowed

        class BoomLedger:
            def is_open_halted(self):
                raise RuntimeError('ledger down')

        ok, reason = _inferred_open_allowed(BoomLedger(), 'c', '2701', {}, 1)
        self.assertFalse(ok)
        self.assertIn('异常', reason)


class InstallStrangleLegPairingPatchTests(unittest.TestCase):
    def setUp(self):
        leg_pairing_mod._INSTALLED = False
        leg_pairing_mod._INSTALL_ERROR = ''

    def test_import_failure_records_error(self):
        with unittest.mock.patch.dict(sys.modules, {'straggle_processor': None}):
            with unittest.mock.patch(
                'builtins.__import__',
                side_effect=ImportError('no straggle'),
            ):
                self.assertFalse(install_strangle_leg_pairing_patch())
        self.assertIn('import straggle_processor 失败', get_install_error())

    def test_install_success_clears_error(self):
        self.assertTrue(install_strangle_leg_pairing_patch())
        self.assertEqual(get_install_error(), '')
        self.assertTrue(getattr(
            __import__('straggle_processor').process_strangle_symbol,
            '_leg_pairing_patched',
            False,
        ))


if __name__ == '__main__':
    unittest.main()
