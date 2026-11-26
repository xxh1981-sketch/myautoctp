"""CTP 合约代码归一：各交易所前缀大小写与码表结构匹配。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ctp_bootstrap  # noqa: F401

from ctp_instrument import (
    apply_exchange_instrument_case,
    canonical_ctp_instrument,
    parse_option_instrument,
    rewrite_ledger_instrument_ids,
)


class _Conn:
    def __init__(self):
        self.quotes = {}
        self.option_quotes = {}
        self.option_info = {}
        self.options_by_strike = {}

    def _normalize_month(self, symbol, month):
        m = str(month)
        if symbol.lower() in {
            'sr', 'cf', 'cy', 'ap', 'cj', 'fg', 'sa', 'ur', 'ma',
            'ta', 'pf', 'rm', 'oi', 'zc', 'sf', 'sm', 'sh', 'pk', 'px',
        } and len(m) == 4:
            return m[1:]
        return m


class TestParseOption(unittest.TestCase):
    def test_dash_and_compact(self):
        dce = parse_option_instrument('C2701-C-2340')
        self.assertEqual(dce['prefix'], 'C')
        self.assertEqual(dce['month'], '2701')
        self.assertEqual(dce['cp'], 'C')
        self.assertEqual(dce['strike'], 2340.0)
        self.assertFalse(dce['ms'])

        compact = parse_option_instrument('SA608C2400')
        self.assertEqual(compact['prefix'], 'SA')
        self.assertEqual(compact['month'], '608')
        self.assertEqual(compact['strike'], 2400.0)

        ms = parse_option_instrument('c2701-MS-C-2320')
        self.assertTrue(ms['ms'])
        self.assertEqual(ms['strike'], 2320.0)

    def test_futures_rejected(self):
        self.assertIsNone(parse_option_instrument('C2701'))
        self.assertIsNone(parse_option_instrument('SA608'))


class TestExchangeCase(unittest.TestCase):
    def test_dce_gfex_shfe_lower_prefix(self):
        self.assertEqual(
            apply_exchange_instrument_case('C2701-C-2340'), 'c2701-C-2340',
        )
        self.assertEqual(
            apply_exchange_instrument_case('P2701-P-7800'), 'p2701-P-7800',
        )
        self.assertEqual(
            apply_exchange_instrument_case('M2701-C-3300'), 'm2701-C-3300',
        )
        self.assertEqual(
            apply_exchange_instrument_case('SI2611-C-9400'), 'si2611-C-9400',
        )
        self.assertEqual(
            apply_exchange_instrument_case('AG2606C6000'), 'ag2606C6000',
        )
        self.assertEqual(
            apply_exchange_instrument_case('SC2409C500'), 'sc2409C500',
        )

    def test_czce_cffex_upper_prefix(self):
        self.assertEqual(
            apply_exchange_instrument_case('fg701C1020'), 'FG701C1020',
        )
        self.assertEqual(
            apply_exchange_instrument_case('sa609C2400'), 'SA609C2400',
        )
        self.assertEqual(
            apply_exchange_instrument_case('io2604-C-4000'), 'IO2604-C-4000',
        )
        self.assertEqual(
            apply_exchange_instrument_case('RM509-C-9000'), 'RM509-C-9000',
        )

    def test_does_not_touch_cp_or_strike(self):
        self.assertEqual(
            apply_exchange_instrument_case('c2701-C-2340'), 'c2701-C-2340',
        )
        self.assertEqual(
            apply_exchange_instrument_case('C2701-MS-C-2320'), 'c2701-MS-C-2320',
        )

    def test_does_not_rewrite_czce_month_width(self):
        """四位郑商所月份不能靠启发式乱切，否则测试/配置里的 SA2608 会被误伤。"""
        self.assertEqual(
            apply_exchange_instrument_case('SA2608C2400'), 'SA2608C2400',
        )


class TestCanonicalLookup(unittest.TestCase):
    def test_quotes_win_over_heuristic(self):
        conn = _Conn()
        conn.quotes = {'c2701-C-2340': object()}
        self.assertEqual(
            canonical_ctp_instrument(conn, 'C2701-C-2340'), 'c2701-C-2340',
        )

    def test_option_info_casefold(self):
        conn = _Conn()
        conn.option_info = {'c': {'c2701-P-2140': {'product_class': '2'}}}
        self.assertEqual(
            canonical_ctp_instrument(conn, 'C2701-P-2140'), 'c2701-P-2140',
        )

    def test_czce_four_digit_via_options_by_strike(self):
        conn = _Conn()
        conn.options_by_strike = {
            'sa': {'608': {2400.0: {'call': 'SA608C2400', 'put': None}}},
        }
        self.assertEqual(
            canonical_ctp_instrument(conn, 'SA2608C2400'), 'SA608C2400',
        )

    def test_ms_not_confused_with_regular(self):
        conn = _Conn()
        conn.option_info = {
            'c': {
                'c2701-C-2320': {'product_class': '2', 'strike_price': 2320},
                'c2701-MS-C-2320': {'product_class': '2', 'strike_price': 2320},
            },
        }
        self.assertEqual(
            canonical_ctp_instrument(conn, 'C2701-MS-C-2320'), 'c2701-MS-C-2320',
        )
        self.assertEqual(
            canonical_ctp_instrument(conn, 'C2701-C-2320'), 'c2701-C-2320',
        )


    def test_magicmock_conn_falls_back_to_heuristic(self):
        from unittest.mock import MagicMock

        self.assertEqual(
            canonical_ctp_instrument(MagicMock(), 'C2701-C-2340'),
            'c2701-C-2340',
        )
    def test_rewrites_positions_not_claims(self):
        conn = _Conn()

        class _Ledger:
            def __init__(self):
                self._data = {
                    'positions': [{
                        'call_instrument': 'C2701-C-2340',
                        'put_instrument': 'C2701-P-2140',
                    }],
                    'leg_claims': {'C2701-C-2340': 1},
                    'unmatched_legs': [
                        {'leg': {'inst': 'SI2611-C-9400'}},
                    ],
                }
                self.saved = 0

            def _save(self):
                self.saved += 1

        ledger = _Ledger()
        n = rewrite_ledger_instrument_ids(ledger, conn)
        self.assertEqual(n, 3)
        self.assertEqual(
            ledger._data['positions'][0]['call_instrument'], 'c2701-C-2340',
        )
        self.assertEqual(
            ledger._data['unmatched_legs'][0]['leg']['inst'], 'si2611-C-9400',
        )
        self.assertEqual(ledger._data['leg_claims']['C2701-C-2340'], 1)
        self.assertEqual(ledger.saved, 1)


if __name__ == '__main__':
    unittest.main()
