"""
V2.7 Entry Decision / 交叉时序 / Exit 优先 / Regime 不平仓。
不改 DoubleMaStrategy_V26.py / execution_core.py。

运行：

    python test_v27_entry.py
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from DoubleMaStrategy_V27 import (
    BB_NEUTRAL,
    ENTRY_ALLOW,
    ENTRY_BLOCK,
    ENTRY_NONE,
    MA_BULL,
    REASON_DIRECTION_MISMATCH,
    REASON_EXIT_PRIORITY,
    REASON_NO_CROSS,
    REASON_NO_TREND,
    REASON_UNAVAILABLE,
    SIGNAL_DEATH,
    SIGNAL_GOLDEN,
    TREND_LONG,
    TREND_RANGE,
    TREND_SHORT,
    TREND_UNAVAILABLE,
    classify_ma_structure,
    decide_final_regime,
    decide_v27_entry,
)


def decide(**kwargs):
    payload = {
        "final_regime": TREND_LONG,
        "golden_cross": False,
        "death_cross": False,
        "trading": True,
        "fixed_size": 1,
    }
    payload.update(kwargs)
    return decide_v27_entry(**payload)


class EntryMatrixTest(unittest.TestCase):
    def test_long_golden_allow(self):
        decision = decide(final_regime=TREND_LONG, golden_cross=True)
        self.assertEqual(decision.action, ENTRY_ALLOW)
        self.assertEqual(decision.signal, SIGNAL_GOLDEN)
        self.assertEqual(decision.reason, SIGNAL_GOLDEN)
        self.assertEqual(decision.target_pos, 1)

    def test_long_death_block(self):
        decision = decide(final_regime=TREND_LONG, death_cross=True)
        self.assertEqual(decision.action, ENTRY_BLOCK)
        self.assertEqual(decision.reason, REASON_DIRECTION_MISMATCH)

    def test_short_death_allow(self):
        decision = decide(final_regime=TREND_SHORT, death_cross=True)
        self.assertEqual(decision.action, ENTRY_ALLOW)
        self.assertEqual(decision.signal, SIGNAL_DEATH)
        self.assertEqual(decision.reason, SIGNAL_DEATH)
        self.assertEqual(decision.target_pos, -1)

    def test_short_golden_block(self):
        decision = decide(final_regime=TREND_SHORT, golden_cross=True)
        self.assertEqual(decision.action, ENTRY_BLOCK)
        self.assertEqual(decision.reason, REASON_DIRECTION_MISMATCH)

    def test_range_golden_block(self):
        decision = decide(final_regime=TREND_RANGE, golden_cross=True)
        self.assertEqual(decision.action, ENTRY_BLOCK)
        self.assertEqual(decision.reason, REASON_NO_TREND)

    def test_range_death_block(self):
        decision = decide(final_regime=TREND_RANGE, death_cross=True)
        self.assertEqual(decision.action, ENTRY_BLOCK)
        self.assertEqual(decision.reason, REASON_NO_TREND)

    def test_unavailable_golden_block(self):
        decision = decide(final_regime=TREND_UNAVAILABLE, golden_cross=True)
        self.assertEqual(decision.action, ENTRY_BLOCK)
        self.assertEqual(decision.reason, REASON_UNAVAILABLE)

    def test_unavailable_death_block(self):
        decision = decide(final_regime=TREND_UNAVAILABLE, death_cross=True)
        self.assertEqual(decision.action, ENTRY_BLOCK)
        self.assertEqual(decision.reason, REASON_UNAVAILABLE)


class NoCrossTest(unittest.TestCase):
    def test_all_regimes_no_cross_are_none(self):
        for regime in (TREND_LONG, TREND_SHORT, TREND_RANGE, TREND_UNAVAILABLE):
            decision = decide(final_regime=regime)
            self.assertEqual(decision.action, ENTRY_NONE)
            self.assertEqual(decision.reason, REASON_NO_CROSS)
            self.assertEqual(decision.target_pos, 0)
            self.assertEqual(decision.signal, "")


class CrossNoCacheTest(unittest.TestCase):
    def test_history_cross_not_reused(self):
        first = decide(final_regime=TREND_RANGE, golden_cross=True)
        self.assertEqual(first.action, ENTRY_BLOCK)
        self.assertEqual(first.reason, REASON_NO_TREND)
        later = decide(final_regime=TREND_LONG, golden_cross=False)
        self.assertEqual(later.action, ENTRY_NONE)
        self.assertEqual(later.reason, REASON_NO_CROSS)

    def test_death_history_not_reused(self):
        first = decide(final_regime=TREND_RANGE, death_cross=True)
        self.assertEqual(first.action, ENTRY_BLOCK)
        later = decide(final_regime=TREND_SHORT, death_cross=False)
        self.assertEqual(later.action, ENTRY_NONE)
        self.assertEqual(later.reason, REASON_NO_CROSS)


class RegimeThenCrossTest(unittest.TestCase):
    def test_regime_ready_wait_for_true_cross(self):
        ma = classify_ma_structure(11.0, 10.0, 30.0, 29.0)
        self.assertEqual(ma, MA_BULL)
        golden = 11.0 > 30.0 and 10.0 <= 29.0
        self.assertFalse(golden)
        regime, _reason = decide_final_regime(TREND_LONG, ma, BB_NEUTRAL)
        self.assertEqual(regime, TREND_LONG)
        first = decide(final_regime=regime, golden_cross=False)
        self.assertEqual(first.action, ENTRY_NONE)
        self.assertEqual(first.reason, REASON_NO_CROSS)
        second = decide(final_regime=TREND_LONG, golden_cross=True)
        self.assertEqual(second.action, ENTRY_ALLOW)
        self.assertEqual(second.target_pos, 1)


class ExitPriorityTest(unittest.TestCase):
    def test_exit_blocks_entry_same_bar(self):
        decision = decide(
            final_regime=TREND_LONG,
            golden_cross=True,
            exit_triggered=True,
        )
        self.assertEqual(decision.action, ENTRY_NONE)
        self.assertEqual(decision.reason, REASON_EXIT_PRIORITY)


class RegimeDoesNotFlattenTest(unittest.TestCase):
    def test_long_to_range_no_flatten_order(self):
        ready, _ = decide_final_regime(TREND_LONG, MA_BULL, BB_NEUTRAL)
        self.assertEqual(ready, TREND_LONG)
        later, _ = decide_final_regime(TREND_RANGE, MA_BULL, BB_NEUTRAL)
        self.assertEqual(later, TREND_RANGE)
        decision = decide(final_regime=later)
        self.assertEqual(decision.action, ENTRY_NONE)
        self.assertEqual(decision.reason, REASON_NO_CROSS)

    def test_long_to_short_without_cross_does_not_reverse(self):
        decision = decide(final_regime=TREND_SHORT)
        self.assertEqual(decision.action, ENTRY_NONE)
        self.assertEqual(decision.reason, REASON_NO_CROSS)
        self.assertNotEqual(decision.action, ENTRY_ALLOW)


if __name__ == "__main__":
    unittest.main()
