"""strangle_rebalance_close_only unit tests."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ctp_bootstrap  # noqa: F401

from straggle_ledger import StrangleLedger
from strangle_rebalance_close_only import (
    CLOSE_KINDS,
    run_close_only_rebalance,
)


class FakeExecutor:
    """Executor stub that consumes ``ledger._data['unmatched_legs']`` directly."""

    def __init__(self, *, consume_count=None, raise_after=None):
        self.calls = 0
        self.seen_kinds = []
        self.consume_count = consume_count
        self.raise_after = raise_after

    def run_rebalance(self, tradeinfo_by_key):
        self.calls += 1
        # Emulate StrangleExecutor: list_unmatched_legs is the input snapshot;
        # we simulate consuming the first N items in-place to mimic
        # successful re-balance handling.
        if self.raise_after is not None and self.calls > self.raise_after:
            raise RuntimeError('boom')
        # Access ledger via tradeinfo_by_key trick: not possible. Instead
        # callers will pass the ledger via global; for the test we install
        # a sentinel on the instance.
        ledger = self.ledger
        legs = list(ledger._data.get('unmatched_legs') or [])
        self.seen_kinds.append([leg.get('kind') for leg in legs])
        if self.consume_count is None:
            consumed = len(legs)
        else:
            consumed = min(self.consume_count, len(legs))
        with ledger._lock:
            ledger._data['unmatched_legs'] = legs[consumed:]
            ledger._save()


class TestRunCloseOnlyRebalance(unittest.TestCase):

    def _make_ledger(self, tmpdir, legs):
        led = StrangleLedger(os.path.join(tmpdir, 'sl.json'))
        for leg in legs:
            led.add_unmatched_leg(leg)
        return led

    def test_only_close_items_visible_to_executor(self):
        with tempfile.TemporaryDirectory() as tmp:
            led = self._make_ledger(tmp, [
                {'symbol': 'sa', 'month': '2608', 'kind': 'awaiting_phase2'},
                {'symbol': 'sa', 'month': '2608', 'kind': 'close_chp_pending'},
                {'symbol': 'cu', 'month': '2608', 'kind': 'awaiting_phase2'},
            ])
            ex = FakeExecutor(consume_count=0)
            ex.ledger = led
            run_close_only_rebalance(ex, led, {})
            self.assertEqual(ex.seen_kinds, [['close_chp_pending']])
            kinds_after = [
                leg.get('kind') for leg in led.list_unmatched_legs()
            ]
            self.assertCountEqual(
                kinds_after,
                ['close_chp_pending', 'awaiting_phase2', 'awaiting_phase2'],
            )

    def test_handled_count_when_executor_consumes(self):
        with tempfile.TemporaryDirectory() as tmp:
            led = self._make_ledger(tmp, [
                {'symbol': 'sa', 'month': '2608', 'kind': 'awaiting_phase2'},
                {'symbol': 'sa', 'month': '2608', 'kind': 'close_chp_pending'},
                {'symbol': 'cu', 'month': '2608', 'kind': 'close_chp_pending'},
            ])
            ex = FakeExecutor(consume_count=2)
            ex.ledger = led
            handled = run_close_only_rebalance(ex, led, {})
            self.assertEqual(handled, 2)
            remain_kinds = [
                leg.get('kind') for leg in led.list_unmatched_legs()
            ]
            self.assertEqual(remain_kinds, ['awaiting_phase2'])

    def test_no_close_items_is_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            led = self._make_ledger(tmp, [
                {'symbol': 'sa', 'month': '2608', 'kind': 'awaiting_phase2'},
            ])
            ex = FakeExecutor(consume_count=1)
            ex.ledger = led
            handled = run_close_only_rebalance(ex, led, {})
            self.assertEqual(handled, 0)
            self.assertEqual(ex.calls, 0)
            self.assertEqual(
                [leg['kind'] for leg in led.list_unmatched_legs()],
                ['awaiting_phase2'],
            )

    def test_restores_other_items_on_executor_exception(self):
        with tempfile.TemporaryDirectory() as tmp:
            led = self._make_ledger(tmp, [
                {'symbol': 'sa', 'month': '2608', 'kind': 'awaiting_phase2'},
                {'symbol': 'sa', 'month': '2608', 'kind': 'close_chp_pending'},
            ])
            ex = FakeExecutor(raise_after=0)
            ex.ledger = led
            with self.assertRaises(RuntimeError):
                run_close_only_rebalance(ex, led, {})
            kinds = sorted(
                leg.get('kind') for leg in led.list_unmatched_legs()
            )
            self.assertEqual(kinds, ['awaiting_phase2', 'close_chp_pending'])

    def test_close_kinds_constant(self):
        self.assertIn('close_chp_pending', CLOSE_KINDS)


if __name__ == '__main__':
    unittest.main()
