"""
V2.7 BB / MA Structure / Final Regime 单元测试。
不改 DoubleMaStrategy_V26.py / execution_core.py。

运行：

    python test_v27_regime.py
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from DoubleMaStrategy_V27 import (
    BB_COMPRESSION_HOLD,
    BB_COMPRESSION_RELEASE,
    BB_DIR_BEAR,
    BB_EXPANSION,
    BB_NEUTRAL,
    BB_UNAVAILABLE,
    MA_BEAR,
    MA_BULL,
    MA_NEUTRAL,
    MA_UNAVAILABLE,
    REASON_ADX_RANGE,
    REASON_REGIME_LONG,
    REASON_REGIME_LONG_BB_HOLD,
    REASON_REGIME_LONG_MA_CONFLICT,
    REASON_REGIME_LONG_MA_NEUTRAL,
    REASON_REGIME_SHORT,
    REASON_REGIME_SHORT_BB_HOLD,
    REASON_REGIME_SHORT_MA_CONFLICT,
    REASON_REGIME_SHORT_MA_NEUTRAL,
    REASON_REGIME_UNAVAILABLE,
    TREND_LONG,
    TREND_RANGE,
    TREND_SHORT,
    TREND_UNAVAILABLE,
    array_manager_size,
    calculate_bb_percentile,
    calculate_bb_width,
    classify_bb_direction,
    classify_bb_state,
    classify_bb_zone,
    classify_ma_structure,
    count_bw_release_streak,
    decide_final_regime,
    invalid_bb_config,
)


def ranked_window(rank: int, lookback: int = 120, now: float = 2.0) -> list:
    """使 count(BW_i <= now) == rank，当前值放在最后。"""
    below = rank - 1
    above = lookback - rank
    return [1.0] * below + [3.0] * above + [now]


class ArrayManagerSizeTest(unittest.TestCase):
    def test_bb_lookback_requires_145(self):
        self.assertEqual(array_manager_size(10, 20, 22, 14, 26, 120), 145)

    def test_larger_adx_still_wins_if_needed(self):
        self.assertEqual(array_manager_size(10, 20, 22, 80, 26, 120), 161)


class BbWidthTest(unittest.TestCase):
    def test_width_formula(self):
        self.assertAlmostEqual(calculate_bb_width(12.0, 8.0, 10.0), 0.4)

    def test_middle_zero_invalid(self):
        self.assertIsNone(calculate_bb_width(1.0, -1.0, 0.0))

    def test_nan_invalid(self):
        self.assertIsNone(calculate_bb_width(float("nan"), 8.0, 10.0))

    def test_inf_invalid(self):
        self.assertIsNone(calculate_bb_width(12.0, 8.0, float("inf")))


class BbPercentileTest(unittest.TestCase):
    def test_full_120_samples(self):
        values = ranked_window(24)
        self.assertEqual(len(values), 120)
        self.assertAlmostEqual(calculate_bb_percentile(values, 120), 0.20)

    def test_119_samples_unavailable(self):
        self.assertIsNone(calculate_bb_percentile([1.0] * 119, 120))

    def test_rank_24_is_compression(self):
        percentile = calculate_bb_percentile(ranked_window(24), 120)
        self.assertAlmostEqual(percentile, 0.20)
        self.assertEqual(classify_bb_state(percentile, 0, 2, 20.0, 80.0), BB_COMPRESSION_HOLD)

    def test_rank_25_is_neutral(self):
        percentile = calculate_bb_percentile(ranked_window(25), 120)
        self.assertAlmostEqual(percentile, 25 / 120)
        self.assertEqual(classify_bb_state(percentile, 0, 2, 20.0, 80.0), BB_NEUTRAL)

    def test_rank_96_is_expansion(self):
        percentile = calculate_bb_percentile(ranked_window(96), 120)
        self.assertAlmostEqual(percentile, 0.80)
        self.assertEqual(classify_bb_state(percentile, 0, 2, 20.0, 80.0), BB_EXPANSION)

    def test_rank_95_is_neutral(self):
        percentile = calculate_bb_percentile(ranked_window(95), 120)
        self.assertAlmostEqual(percentile, 95 / 120)
        self.assertEqual(classify_bb_state(percentile, 0, 2, 20.0, 80.0), BB_NEUTRAL)

    def test_includes_current_value(self):
        values = [10.0] * 119 + [0.1]
        self.assertAlmostEqual(calculate_bb_percentile(values, 120), 1 / 120)


class BbZoneBoundaryTest(unittest.TestCase):
    def test_20_00_compression(self):
        self.assertEqual(classify_bb_zone(0.20, 20.0, 80.0), BB_COMPRESSION_HOLD)

    def test_20_01_neutral(self):
        self.assertEqual(classify_bb_zone(0.2001, 20.0, 80.0), BB_NEUTRAL)

    def test_79_99_neutral(self):
        self.assertEqual(classify_bb_zone(0.7999, 20.0, 80.0), BB_NEUTRAL)

    def test_80_00_expansion(self):
        self.assertEqual(classify_bb_zone(0.80, 20.0, 80.0), BB_EXPANSION)


class BbReleaseTest(unittest.TestCase):
    def test_two_rises_are_release(self):
        streak = count_bw_release_streak([10.0, 11.0, 12.0])
        self.assertEqual(streak, 2)
        self.assertEqual(
            classify_bb_state(0.10, streak, 2, 20.0, 80.0),
            BB_COMPRESSION_RELEASE,
        )

    def test_flat_is_hold(self):
        streak = count_bw_release_streak([10.0, 11.0, 11.0])
        self.assertEqual(streak, 0)
        self.assertEqual(classify_bb_state(0.10, streak, 2, 20.0, 80.0), BB_COMPRESSION_HOLD)

    def test_pullback_is_hold(self):
        streak = count_bw_release_streak([10.0, 11.0, 10.0])
        self.assertEqual(streak, 0)
        self.assertEqual(classify_bb_state(0.10, streak, 2, 20.0, 80.0), BB_COMPRESSION_HOLD)

    def test_no_release_latch(self):
        released = classify_bb_state(0.10, count_bw_release_streak([10.0, 11.0, 12.0]), 2, 20.0, 80.0)
        self.assertEqual(released, BB_COMPRESSION_RELEASE)
        held = classify_bb_state(0.10, count_bw_release_streak([10.0, 11.0, 12.0, 12.0]), 2, 20.0, 80.0)
        self.assertEqual(held, BB_COMPRESSION_HOLD)

    def test_parameter_n_is_used(self):
        streak = count_bw_release_streak([10.0, 11.0, 12.0])
        self.assertEqual(classify_bb_state(0.10, streak, 3, 20.0, 80.0), BB_COMPRESSION_HOLD)
        streak3 = count_bw_release_streak([10.0, 11.0, 12.0, 13.0])
        self.assertEqual(streak3, 3)
        self.assertEqual(classify_bb_state(0.10, streak3, 3, 20.0, 80.0), BB_COMPRESSION_RELEASE)

    def test_expansion_not_called_release(self):
        self.assertEqual(
            classify_bb_state(0.85, count_bw_release_streak([10.0, 11.0, 12.0]), 2, 20.0, 80.0),
            BB_EXPANSION,
        )

    def test_neutral_not_called_release(self):
        self.assertEqual(
            classify_bb_state(0.50, count_bw_release_streak([10.0, 11.0, 12.0]), 2, 20.0, 80.0),
            BB_NEUTRAL,
        )


class BbStateMachineTest(unittest.TestCase):
    def test_hold_to_release_to_hold(self):
        hold = classify_bb_state(0.10, 0, 2, 20.0, 80.0)
        release = classify_bb_state(0.10, 2, 2, 20.0, 80.0)
        hold_again = classify_bb_state(0.10, 0, 2, 20.0, 80.0)
        self.assertEqual(hold, BB_COMPRESSION_HOLD)
        self.assertEqual(release, BB_COMPRESSION_RELEASE)
        self.assertEqual(hold_again, BB_COMPRESSION_HOLD)

    def test_release_to_neutral_to_expansion(self):
        self.assertEqual(classify_bb_state(0.10, 2, 2, 20.0, 80.0), BB_COMPRESSION_RELEASE)
        self.assertEqual(classify_bb_state(0.25, 2, 2, 20.0, 80.0), BB_NEUTRAL)
        self.assertEqual(classify_bb_state(0.80, 0, 2, 20.0, 80.0), BB_EXPANSION)

    def test_expansion_to_neutral_to_hold(self):
        self.assertEqual(classify_bb_state(0.85, 0, 2, 20.0, 80.0), BB_EXPANSION)
        self.assertEqual(classify_bb_state(0.50, 0, 2, 20.0, 80.0), BB_NEUTRAL)
        self.assertEqual(classify_bb_state(0.10, 0, 2, 20.0, 80.0), BB_COMPRESSION_HOLD)

    def test_missing_percentile_unavailable(self):
        self.assertEqual(classify_bb_state(None, 2, 2, 20.0, 80.0), BB_UNAVAILABLE)


class BbConfigTest(unittest.TestCase):
    def test_valid_defaults(self):
        self.assertFalse(invalid_bb_config(26, 2.0, 120, 20.0, 80.0, 2))

    def test_release_bars_below_two(self):
        self.assertTrue(invalid_bb_config(26, 2.0, 120, 20.0, 80.0, 1))

    def test_lookback_below_one(self):
        self.assertTrue(invalid_bb_config(26, 2.0, 0, 20.0, 80.0, 2))

    def test_expansion_not_greater_than_compression(self):
        self.assertTrue(invalid_bb_config(26, 2.0, 120, 20.0, 20.0, 2))


class MaStructureTest(unittest.TestCase):
    def test_bull_both_rising(self):
        self.assertEqual(classify_ma_structure(11.0, 10.0, 21.0, 20.0), MA_BULL)

    def test_bear_both_falling(self):
        self.assertEqual(classify_ma_structure(9.0, 10.0, 19.0, 20.0), MA_BEAR)

    def test_fast_up_slow_down_neutral(self):
        self.assertEqual(classify_ma_structure(11.0, 10.0, 19.0, 20.0), MA_NEUTRAL)

    def test_fast_down_slow_up_neutral(self):
        self.assertEqual(classify_ma_structure(9.0, 10.0, 21.0, 20.0), MA_NEUTRAL)

    def test_fast_up_slow_flat_neutral(self):
        self.assertEqual(classify_ma_structure(11.0, 10.0, 20.0, 20.0), MA_NEUTRAL)

    def test_fast_flat_slow_down_neutral(self):
        self.assertEqual(classify_ma_structure(10.0, 10.0, 19.0, 20.0), MA_NEUTRAL)

    def test_nan_unavailable(self):
        self.assertEqual(classify_ma_structure(float("nan"), 10.0, 21.0, 20.0), MA_UNAVAILABLE)

    def test_bull_does_not_require_fast_above_slow(self):
        self.assertEqual(classify_ma_structure(11.0, 10.0, 30.0, 29.0), MA_BULL)


class FinalRegimeTest(unittest.TestCase):
    def test_adx_range_all_valid_bb(self):
        for bb in (BB_COMPRESSION_HOLD, BB_COMPRESSION_RELEASE, BB_NEUTRAL, BB_EXPANSION):
            for ma in (MA_BULL, MA_BEAR, MA_NEUTRAL):
                regime, reason = decide_final_regime(TREND_RANGE, ma, bb)
                self.assertEqual(regime, TREND_RANGE)
                self.assertEqual(reason, REASON_ADX_RANGE)

    def test_long_with_allowing_bb(self):
        for bb, _label in (
            (BB_COMPRESSION_RELEASE, "release"),
            (BB_NEUTRAL, "neutral"),
            (BB_EXPANSION, "expansion"),
        ):
            regime, reason = decide_final_regime(TREND_LONG, MA_BULL, bb)
            self.assertEqual(regime, TREND_LONG, _label)
            self.assertEqual(reason, REASON_REGIME_LONG, _label)

    def test_long_hold_is_range(self):
        regime, reason = decide_final_regime(TREND_LONG, MA_BULL, BB_COMPRESSION_HOLD)
        self.assertEqual(regime, TREND_RANGE)
        self.assertEqual(reason, REASON_REGIME_LONG_BB_HOLD)

    def test_short_with_allowing_bb(self):
        for bb in (BB_COMPRESSION_RELEASE, BB_NEUTRAL, BB_EXPANSION):
            regime, reason = decide_final_regime(TREND_SHORT, MA_BEAR, bb)
            self.assertEqual(regime, TREND_SHORT)
            self.assertEqual(reason, REASON_REGIME_SHORT)

    def test_short_hold_is_range(self):
        regime, reason = decide_final_regime(TREND_SHORT, MA_BEAR, BB_COMPRESSION_HOLD)
        self.assertEqual(regime, TREND_RANGE)
        self.assertEqual(reason, REASON_REGIME_SHORT_BB_HOLD)

    def test_direction_conflict_all_bb(self):
        for bb in (BB_COMPRESSION_HOLD, BB_COMPRESSION_RELEASE, BB_NEUTRAL, BB_EXPANSION):
            regime, reason = decide_final_regime(TREND_LONG, MA_BEAR, bb)
            self.assertEqual(regime, TREND_RANGE)
            self.assertEqual(reason, REASON_REGIME_LONG_MA_CONFLICT)
            regime, reason = decide_final_regime(TREND_SHORT, MA_BULL, bb)
            self.assertEqual(regime, TREND_RANGE)
            self.assertEqual(reason, REASON_REGIME_SHORT_MA_CONFLICT)

    def test_ma_neutral_all_bb(self):
        for bb in (BB_COMPRESSION_HOLD, BB_COMPRESSION_RELEASE, BB_NEUTRAL, BB_EXPANSION):
            regime, reason = decide_final_regime(TREND_LONG, MA_NEUTRAL, bb)
            self.assertEqual(regime, TREND_RANGE)
            self.assertEqual(reason, REASON_REGIME_LONG_MA_NEUTRAL)
            regime, reason = decide_final_regime(TREND_SHORT, MA_NEUTRAL, bb)
            self.assertEqual(regime, TREND_RANGE)
            self.assertEqual(reason, REASON_REGIME_SHORT_MA_NEUTRAL)

    def test_unavailable_priority(self):
        self.assertEqual(
            decide_final_regime(TREND_UNAVAILABLE, MA_BULL, BB_NEUTRAL)[0],
            TREND_UNAVAILABLE,
        )
        self.assertEqual(
            decide_final_regime(TREND_LONG, MA_UNAVAILABLE, BB_NEUTRAL)[0],
            TREND_UNAVAILABLE,
        )
        self.assertEqual(
            decide_final_regime(TREND_LONG, MA_BULL, BB_UNAVAILABLE)[0],
            TREND_UNAVAILABLE,
        )
        regime, reason = decide_final_regime(TREND_RANGE, MA_BULL, BB_UNAVAILABLE)
        self.assertEqual(regime, TREND_UNAVAILABLE)
        self.assertEqual(reason, REASON_REGIME_UNAVAILABLE)

    def test_direction_bias_not_in_matrix(self):
        self.assertEqual(classify_bb_direction(90.0, 100.0), BB_DIR_BEAR)
        regime, reason = decide_final_regime(TREND_LONG, MA_BULL, BB_NEUTRAL)
        self.assertEqual(regime, TREND_LONG)
        self.assertEqual(reason, REASON_REGIME_LONG)


if __name__ == "__main__":
    unittest.main()
