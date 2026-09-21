"""
DoubleMaStrategy V2.6

在 V2.5 上最小增量：ADX/DMI Trend Filter + Double MA Entry Trigger。
不覆盖 V23 / V24 / V241 / V25。不改 execution_core.py / wqk_ctp_gateway.py。
不改 V2.5 Exit Engine（Fixed TP / Initial SL / Chandelier）。

ADX/DMI 只过滤新开仓，不负责平仓。出场仍只 set_target_pos(0) + drive_execution。
拷进 VeighNa Station 时：本文件 + execution_core.py + wqk_ctp_gateway.py。
"""

import math
import os
import sys
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional, Set, Tuple

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    # 保证同目录的 execution_core.py 能被 import（VeighNa Station 工作目录不一定是策略目录）
    sys.path.insert(0, _THIS_DIR)

from vnpy.trader.constant import Direction, Offset
from vnpy_ctastrategy import (
    CtaTemplate,
    StopOrder,
    TickData,
    BarData,
    TradeData,
    OrderData,
    BarGenerator,
    ArrayManager,
)

from execution_core import (
    ActionKind,
    ExecutionCore,
    OffsetKind,
    OrderAction,
    OrderIntent,
    OrderSnapshot,
    OrderStatus,
    PositionSnapshot,
    RecoveryReason,
    SystemState,
    TradingState,
    default_trade_ledger_path,
    infer_direction_kind,
    infer_offset_kind,
    infer_order_intent,
)


# ============================================================
# V2.5 策略层：Tracker + Exit Engine（不进 ExecutionCore）
# ============================================================

MODE_IMMEDIATE = "IMMEDIATE"
MODE_PROFIT_ONLY = "PROFIT_ONLY"
PHASE_ENTRY_BAR = "ENTRY_BAR"
PHASE_FIRST_FULL_BAR = "FIRST_FULL_BAR"
PHASE_NORMAL = "NORMAL_TRACKING"
SOURCE_FIXED_TP = "FIXED_TP"
SOURCE_INITIAL_SL = "INITIAL_SL"
SOURCE_CHANDELIER = "CHANDELIER"

class ExitReason(Enum):
    """面板/日志用出场原因。均线交叉不写入这里。"""
    NONE = "NONE"
    TAKE_PROFIT = "TAKE_PROFIT"
    STOP_LOSS = "STOP_LOSS"


@dataclass
class BarExitResult:
    """Exit Engine 轻量结果。不是订单、不是状态机。"""
    triggered: bool = False
    reason: str = ExitReason.NONE.value
    source: str = ""
    entry_price: float = 0.0
    trigger_price: float = 0.0
    close_price: float = 0.0
    tp_price: float = 0.0
    sl_price: float = 0.0
    initial_sl: float = 0.0
    dynamic_stop: float = 0.0
    raw_stop: float = 0.0
    effective_stop: float = 0.0
    atr: float = 0.0
    highest_high: float = 0.0
    lowest_low: float = 0.0
    phase: str = ""
    monotonic_clamp: bool = False
    unavailable: bool = False
    unavailable_reason: str = ""
    chandelier_unavailable: bool = False
    log_entry: bool = False
    log_chandelier: bool = False


class EntryPriceTracker:
    """
    只根据 TradeData 跟踪入场均价。
    strategy_pos 仍是唯一仓位权威；tracked_volume 只做校验。
    禁止发单、禁止改 target/strategy_pos。
    """

    def __init__(self) -> None:
        self.avg_entry_price: Optional[float] = None
        self.tracked_volume: int = 0
        self._seen: Set[str] = set()

    def clear_position(self) -> None:
        """全平后清均价和校验手数；已处理成交号保留，避免重复计入。"""
        self.avg_entry_price = None
        self.tracked_volume = 0

    def invalidate(self) -> None:
        """非成交改仓后丢掉可能过期的均价。不清 _seen。"""
        self.avg_entry_price = None
        self.tracked_volume = 0

    def record(self, trade) -> str:
        vt_tradeid = str(getattr(trade, "vt_tradeid", "") or "")
        if not vt_tradeid:
            return "EntryPriceTracker skip: empty vt_tradeid"
        if vt_tradeid in self._seen:
            return f"EntryPriceTracker duplicate ignored: {vt_tradeid}"

        price = float(getattr(trade, "price", 0) or 0)
        volume = int(getattr(trade, "volume", 0) or 0)
        if price <= 0 or volume <= 0:
            return f"EntryPriceTracker skip invalid price/volume: {vt_tradeid}"

        intent = infer_order_intent(
            getattr(trade, "direction", ""),
            getattr(trade, "offset", ""),
        )
        if intent == OrderIntent.UNKNOWN:
            return f"EntryPriceTracker skip unknown offset/direction: {vt_tradeid}"

        self._seen.add(vt_tradeid)
        old_vol = int(self.tracked_volume)
        old_abs = abs(old_vol)
        old_avg = self.avg_entry_price

        if intent == OrderIntent.OPEN_LONG:
            self._apply_open(+volume, price, old_vol, old_abs, old_avg)
        elif intent == OrderIntent.OPEN_SHORT:
            self._apply_open(-volume, price, old_vol, old_abs, old_avg)
        elif intent == OrderIntent.CLOSE_LONG:
            self.tracked_volume = old_vol - volume
            if self.tracked_volume <= 0:
                self.tracked_volume = 0
                self.avg_entry_price = None
        elif intent == OrderIntent.CLOSE_SHORT:
            self.tracked_volume = old_vol + volume
            if self.tracked_volume >= 0:
                self.tracked_volume = 0
                self.avg_entry_price = None

        avg_text = "None" if self.avg_entry_price is None else f"{self.avg_entry_price:.6f}"
        return (
            f"EntryPriceTracker vt_tradeid={vt_tradeid} intent={intent.value} "
            f"price={price} volume={volume} tracked_volume={self.tracked_volume} "
            f"avg_entry_price={avg_text}"
        )

    def _apply_open(
        self,
        signed_volume: int,
        price: float,
        old_vol: int,
        old_abs: int,
        old_avg: Optional[float],
    ) -> None:
        add_abs = abs(int(signed_volume))
        same_side = (old_vol == 0) or (old_vol > 0 and signed_volume > 0) or (
            old_vol < 0 and signed_volume < 0
        )
        if same_side:
            new_abs = old_abs + add_abs
            if old_abs == 0 or old_avg is None:
                self.avg_entry_price = price
            else:
                self.avg_entry_price = (old_avg * old_abs + price * add_abs) / new_abs
            self.tracked_volume = old_vol + signed_volume
            return
        # 本策略会先平光再反向；若出现对向开仓，剩余仓用新成交价
        self.tracked_volume = old_vol + signed_volume
        if self.tracked_volume == 0:
            self.avg_entry_price = None
        elif (old_vol > 0 and self.tracked_volume > 0) or (
            old_vol < 0 and self.tracked_volume < 0
        ):
            return
        else:
            self.avg_entry_price = price


def parse_close_price(close_price) -> Optional[float]:
    """有效收盘价：有限、能转 float、且 > 0。None / 0 / 负 / 非数字 / NaN / Inf 都无效。"""
    if close_price is None:
        return None
    try:
        value = float(close_price)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return value


def parse_bar_close_price(bar) -> Optional[float]:
    return parse_close_price(getattr(bar, "close_price", None))


def parse_bar_high_price(bar) -> Optional[float]:
    return parse_close_price(getattr(bar, "high_price", None))


def parse_bar_low_price(bar) -> Optional[float]:
    return parse_close_price(getattr(bar, "low_price", None))


def maybe_invalidate_tracker(tracker: EntryPriceTracker, strategy_pos: int) -> bool:
    """
    仅用于同步/adopt 之后。手数一致则不动。
    禁止在 on_bar / on_order / on_trade 里调用。
    """
    if int(tracker.tracked_volume) == int(strategy_pos):
        return False
    tracker.invalidate()
    return True


def next_tpsl_log(last_key: str, unavailable: bool, reason: str = "") -> Tuple[str, Optional[str]]:
    """同一 unavailable 原因静默；从 unavailable 恢复时打一条 available again。"""
    if unavailable:
        key = f"unavailable:{reason}"
        if key == last_key:
            return key, None
        return key, reason
    key = "available"
    if str(last_key).startswith("unavailable:"):
        return key, "[V2.5 EXIT] available again"
    return key, None


def array_manager_size(
    fast_window: int,
    slow_window: int,
    atr_period: int,
    adx_period: int = 14,
) -> int:
    """均线 / ATR / ADX 预热取更大者。TA-Lib ADX(n) 约需 2*n+1 根。"""
    ma_need = max(int(fast_window or 1), int(slow_window or 1)) * 2
    atr_need = max(int(atr_period or 1), 1) + 2
    adx_need = 2 * max(int(adx_period or 1), 1) + 1
    return max(ma_need, atr_need, adx_need)


# ============================================================
# V2.6 Trend Filter（只过滤 Entry，不改 Exit Engine / Core）
# ============================================================

TREND_UNAVAILABLE = "UNAVAILABLE"
TREND_RANGE = "RANGE"
TREND_LONG = "LONG"
TREND_SHORT = "SHORT"
CANDIDATE_NONE = "NONE"
CANDIDATE_LONG = "LONG"
CANDIDATE_SHORT = "SHORT"
SIGNAL_GOLDEN = "GOLDEN_CROSS"
SIGNAL_DEATH = "DEATH_CROSS"
ENTRY_ALLOW = "ALLOW"
ENTRY_BLOCK = "BLOCK"
ENTRY_NONE = "NONE"
REASON_NO_TREND = "NO_TREND"
REASON_UNAVAILABLE = "UNAVAILABLE"
REASON_DIRECTION_MISMATCH = "DIRECTION_MISMATCH"
REASON_NO_CROSS = "NO_CROSS"
REASON_NOT_TRADING = "NOT_TRADING"
REASON_EXIT_PRIORITY = "EXIT_PRIORITY"
REASON_INVALID_CONFIG = "INVALID_CONFIG"


def parse_indicator_value(value) -> Optional[float]:
    """ADX/DI：有限浮点即可（含 0）。NaN / Inf / 非数字 → None。"""
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed):
        return None
    return parsed


def invalid_trend_config(adx_threshold, trend_confirmation_bars) -> bool:
    try:
        threshold = float(adx_threshold)
        bars = int(trend_confirmation_bars)
    except (TypeError, ValueError):
        return True
    if not math.isfinite(threshold) or threshold <= 0:
        return True
    if bars < 1:
        return True
    return False


@dataclass
class TrendFilter:
    """ADX/DMI 趋势状态。确认计数在 UNAVAILABLE / RANGE 时必须清零。"""
    state: str = TREND_UNAVAILABLE
    long_confirm: int = 0
    short_confirm: int = 0
    candidate: str = CANDIDATE_NONE
    adx: float = 0.0
    plus_di: float = 0.0
    minus_di: float = 0.0

    def reset_unavailable(self) -> None:
        self.state = TREND_UNAVAILABLE
        self.long_confirm = 0
        self.short_confirm = 0
        self.candidate = CANDIDATE_NONE
        self.adx = 0.0
        self.plus_di = 0.0
        self.minus_di = 0.0


@dataclass
class TrendTickResult:
    state: str = TREND_UNAVAILABLE
    candidate: str = CANDIDATE_NONE
    long_confirm: int = 0
    short_confirm: int = 0
    confirm_needed: int = 2
    log_trend: bool = False
    invalid_config: bool = False
    prev_state: str = TREND_UNAVAILABLE
    adx: float = 0.0
    plus_di: float = 0.0
    minus_di: float = 0.0


def _trend_should_log(
    prev_state: str,
    prev_long: int,
    prev_short: int,
    filt: TrendFilter,
) -> bool:
    if filt.state != prev_state:
        return True
    if (filt.long_confirm, filt.short_confirm) != (prev_long, prev_short):
        return True
    return False


def update_trend_filter(
    filt: TrendFilter,
    *,
    adx,
    plus_di,
    minus_di,
    adx_threshold,
    trend_confirmation_bars,
) -> TrendTickResult:
    """
    每根完整 K 更新一次。非法参数或非有限 ADX/DI → UNAVAILABLE 并清零确认。
    已确认 LONG/SHORT 不因一根反向 DI 立即改变；连续确认满额可直接 LONG↔SHORT。
    """
    prev_state = filt.state
    prev_long = int(filt.long_confirm)
    prev_short = int(filt.short_confirm)
    result = TrendTickResult(prev_state=prev_state)

    if invalid_trend_config(adx_threshold, trend_confirmation_bars):
        filt.reset_unavailable()
        result.state = filt.state
        result.candidate = CANDIDATE_NONE
        result.invalid_config = True
        result.log_trend = _trend_should_log(prev_state, prev_long, prev_short, filt)
        return result

    threshold = float(adx_threshold)
    bars = int(trend_confirmation_bars)
    result.confirm_needed = bars

    parsed_adx = parse_indicator_value(adx)
    parsed_plus = parse_indicator_value(plus_di)
    parsed_minus = parse_indicator_value(minus_di)
    if parsed_adx is None or parsed_plus is None or parsed_minus is None:
        filt.reset_unavailable()
        result.state = filt.state
        result.candidate = CANDIDATE_NONE
        result.log_trend = _trend_should_log(prev_state, prev_long, prev_short, filt)
        return result

    filt.adx = parsed_adx
    filt.plus_di = parsed_plus
    filt.minus_di = parsed_minus
    result.adx = parsed_adx
    result.plus_di = parsed_plus
    result.minus_di = parsed_minus

    if parsed_adx < threshold:
        filt.state = TREND_RANGE
        filt.long_confirm = 0
        filt.short_confirm = 0
        filt.candidate = CANDIDATE_NONE
    elif parsed_plus > parsed_minus:
        filt.candidate = CANDIDATE_LONG
        filt.long_confirm = min(int(filt.long_confirm) + 1, bars)
        filt.short_confirm = 0
        if filt.long_confirm >= bars:
            filt.state = TREND_LONG
    elif parsed_minus > parsed_plus:
        filt.candidate = CANDIDATE_SHORT
        filt.short_confirm = min(int(filt.short_confirm) + 1, bars)
        filt.long_confirm = 0
        if filt.short_confirm >= bars:
            filt.state = TREND_SHORT
    else:
        filt.candidate = CANDIDATE_NONE
        filt.long_confirm = 0
        filt.short_confirm = 0

    result.state = filt.state
    result.candidate = filt.candidate
    result.long_confirm = int(filt.long_confirm)
    result.short_confirm = int(filt.short_confirm)
    result.log_trend = _trend_should_log(prev_state, prev_long, prev_short, filt)
    return result


@dataclass
class EntryDecision:
    action: str = ENTRY_NONE
    reason: str = ""
    signal: str = ""
    trend: str = TREND_UNAVAILABLE
    target_pos: int = 0


def decide_v26_entry(
    *,
    trend_state: str,
    golden_cross: bool,
    death_cross: bool,
    trading: bool,
    fixed_size: int,
    exit_triggered: bool = False,
) -> EntryDecision:
    """Trend 给权限，真交叉才开仓。Exit 本根优先，历史回放 (trading=False) 不下单。"""
    signal = ""
    if golden_cross:
        signal = SIGNAL_GOLDEN
    elif death_cross:
        signal = SIGNAL_DEATH
    decision = EntryDecision(signal=signal, trend=str(trend_state or TREND_UNAVAILABLE))
    if exit_triggered:
        decision.action = ENTRY_NONE
        decision.reason = REASON_EXIT_PRIORITY
        return decision
    if not signal:
        decision.reason = REASON_NO_CROSS
        return decision
    if not trading:
        decision.reason = REASON_NOT_TRADING
        return decision

    size = abs(int(fixed_size or 0))
    if golden_cross:
        if trend_state == TREND_LONG:
            decision.action = ENTRY_ALLOW
            decision.target_pos = size
            return decision
        if trend_state == TREND_SHORT:
            decision.action = ENTRY_BLOCK
            decision.reason = REASON_DIRECTION_MISMATCH
            return decision
        if trend_state == TREND_RANGE:
            decision.action = ENTRY_BLOCK
            decision.reason = REASON_NO_TREND
            return decision
        decision.action = ENTRY_BLOCK
        decision.reason = REASON_UNAVAILABLE
        return decision

    if trend_state == TREND_SHORT:
        decision.action = ENTRY_ALLOW
        decision.target_pos = -size
        return decision
    if trend_state == TREND_LONG:
        decision.action = ENTRY_BLOCK
        decision.reason = REASON_DIRECTION_MISMATCH
        return decision
    if trend_state == TREND_RANGE:
        decision.action = ENTRY_BLOCK
        decision.reason = REASON_NO_TREND
        return decision
    decision.action = ENTRY_BLOCK
    decision.reason = REASON_UNAVAILABLE
    return decision


def minute_key(dt) -> Optional[Tuple]:
    """用年月日时分识别同一根 1 分钟 K 线，避免时区干扰。"""
    if dt is None:
        return None
    try:
        return (int(dt.year), int(dt.month), int(dt.day), int(dt.hour), int(dt.minute))
    except Exception:
        return None


def align_stop_to_tick(price: float, pricetick: float, side: int) -> float:
    """多头止损向下对齐 tick；空头止损向上对齐 tick。不用 round。"""
    tick = float(pricetick)
    raw = float(price)
    if tick <= 0:
        return raw
    ratio = raw / tick
    if int(side) > 0:
        return math.floor(ratio + 1e-12) * tick
    return math.ceil(ratio - 1e-12) * tick


def normalize_dynamic_mode(mode) -> Optional[str]:
    text = str(mode or "").strip().upper()
    if text in (MODE_IMMEDIATE, MODE_PROFIT_ONLY):
        return text
    return None


def compute_fixed_tp(entry: float, ticks: int, pricetick: float, side: int) -> Optional[float]:
    n = int(ticks)
    if n <= 0:
        return None
    if side > 0:
        return float(entry) + n * float(pricetick)
    return float(entry) - n * float(pricetick)


def compute_initial_sl(entry: float, ticks: int, pricetick: float, side: int) -> Optional[float]:
    n = int(ticks)
    if n <= 0:
        return None
    if side > 0:
        return float(entry) - n * float(pricetick)
    return float(entry) + n * float(pricetick)


def compute_raw_chandelier(
    *,
    side: int,
    highest_high: Optional[float],
    lowest_low: Optional[float],
    atr: Optional[float],
    multiplier: float,
    pricetick: float,
) -> Optional[float]:
    if atr is None or float(atr) <= 0 or float(multiplier) <= 0:
        return None
    distance = float(atr) * float(multiplier)
    if side > 0:
        if highest_high is None:
            return None
        return align_stop_to_tick(float(highest_high) - distance, pricetick, 1)
    if lowest_low is None:
        return None
    return align_stop_to_tick(float(lowest_low) + distance, pricetick, -1)


def apply_monotonic_dynamic_stop(
    side: int,
    previous: Optional[float],
    raw: Optional[float],
) -> Tuple[Optional[float], bool]:
    """返回 (dynamic_stop, 是否被单调性夹紧)。"""
    if raw is None:
        return previous, False
    if previous is None:
        return raw, False
    if side > 0:
        if raw < previous:
            return previous, True
        return raw, False
    if raw > previous:
        return previous, True
    return raw, False


def chandelier_may_trigger(
    mode: str,
    side: int,
    entry: float,
    dynamic_stop: Optional[float],
) -> bool:
    if dynamic_stop is None:
        return False
    if mode == MODE_IMMEDIATE:
        return True
    if side > 0:
        return float(dynamic_stop) >= float(entry)
    return float(dynamic_stop) <= float(entry)


@dataclass
class ExitEngine:
    """
    轻量出场状态。不是第二套订单状态机。
    每次 0→非0 必须 reset 后再 note_new_position。
    """
    side: int = 0
    entry_bar_id: Optional[Tuple] = None
    pending_entry_bar: bool = False
    seen_entry_bar: bool = False
    seen_first_full_bar: bool = False
    highest_high_since_entry: Optional[float] = None
    lowest_low_since_entry: Optional[float] = None
    dynamic_stop: Optional[float] = None
    frozen_entry: Optional[float] = None
    frozen_initial_sl: Optional[float] = None
    frozen_fixed_tp: Optional[float] = None
    logged_entry: bool = False

    def reset(self) -> None:
        self.side = 0
        self.entry_bar_id = None
        self.pending_entry_bar = False
        self.seen_entry_bar = False
        self.seen_first_full_bar = False
        self.highest_high_since_entry = None
        self.lowest_low_since_entry = None
        self.dynamic_stop = None
        self.frozen_entry = None
        self.frozen_initial_sl = None
        self.frozen_fixed_tp = None
        self.logged_entry = False

    def note_new_position(self, side: int, entry_bar_id: Optional[Tuple]) -> None:
        self.reset()
        self.side = 1 if int(side) > 0 else -1
        self.entry_bar_id = entry_bar_id
        self.pending_entry_bar = entry_bar_id is None

    def classify_bar(self, bar_id: Optional[Tuple]) -> str:
        if self.pending_entry_bar:
            return PHASE_ENTRY_BAR
        if self.entry_bar_id is not None and bar_id == self.entry_bar_id:
            return PHASE_ENTRY_BAR
        if not self.seen_first_full_bar:
            return PHASE_FIRST_FULL_BAR
        return PHASE_NORMAL

    def mark_phase_done(self, phase: str, bar_id: Optional[Tuple]) -> None:
        if phase == PHASE_ENTRY_BAR:
            self.seen_entry_bar = True
            self.pending_entry_bar = False
            if self.entry_bar_id is None:
                self.entry_bar_id = bar_id
            return
        if phase == PHASE_FIRST_FULL_BAR:
            self.seen_first_full_bar = True
            self.seen_entry_bar = True


def evaluate_v25_exit(
    *,
    close_price,
    high_price,
    low_price,
    bar_id: Optional[Tuple],
    strategy_pos: int,
    target_pos: int,
    tracked_volume: int,
    avg_entry_price: Optional[float],
    pricetick: Optional[float],
    atr: Optional[float],
    fixed_tp_ticks: int,
    initial_sl_ticks: int,
    chandelier_enabled: bool,
    atr_multiplier: float,
    dynamic_stop_mode: str,
    system_allows_trade: bool,
    has_active_orders: bool,
    engine: ExitEngine,
) -> BarExitResult:
    """
    仓位稳住后用收盘价判断 Fixed TP / Effective Stop。
    high/low 只更新 Since Entry；不作为触发价。
    """
    parsed_close = parse_close_price(close_price)
    result = BarExitResult(close_price=float(parsed_close) if parsed_close is not None else 0.0)
    tp_ticks = int(fixed_tp_ticks or 0)
    sl_ticks = int(initial_sl_ticks or 0)
    chandelier_on = bool(chandelier_enabled)
    mode = normalize_dynamic_mode(dynamic_stop_mode) or ""

    if tp_ticks < 0:
        tp_ticks = 0
        result.unavailable = True
        result.unavailable_reason = "[V2.5 EXIT] unavailable reason=INVALID_FIXED_TP_TICKS"
        return result
    if sl_ticks < 0:
        result.unavailable = True
        result.unavailable_reason = "[V2.5 EXIT] unavailable reason=INVALID_INITIAL_SL_TICKS"
        return result
    if chandelier_on and (float(atr_multiplier) <= 0 or normalize_dynamic_mode(dynamic_stop_mode) is None):
        chandelier_on = False
        result.chandelier_unavailable = True

    if tp_ticks == 0 and sl_ticks == 0 and not chandelier_on:
        return result
    if parsed_close is None:
        result.unavailable = True
        result.unavailable_reason = "[V2.5 EXIT] invalid close_price, skip TP/SL"
        return result
    if not system_allows_trade or has_active_orders:
        return result
    if int(strategy_pos) == 0 or int(strategy_pos) != int(target_pos):
        return result
    if int(tracked_volume) != int(strategy_pos):
        result.unavailable = True
        result.unavailable_reason = "[V2.5 EXIT] unavailable reason=TRACKER_MISMATCH"
        return result
    if avg_entry_price is None or float(avg_entry_price) <= 0:
        result.unavailable = True
        result.unavailable_reason = "[V2.5 EXIT] unavailable reason=ENTRY_UNAVAILABLE"
        return result
    if pricetick is None or float(pricetick) <= 0:
        result.unavailable = True
        result.unavailable_reason = "[V2.5 EXIT] unavailable reason=PRICETICK_UNAVAILABLE"
        return result
    if engine.side == 0:
        result.unavailable = True
        result.unavailable_reason = "[V2.5 EXIT] unavailable reason=ENTRY_UNAVAILABLE"
        return result

    side = 1 if int(strategy_pos) > 0 else -1
    entry = float(engine.frozen_entry) if engine.frozen_entry is not None else float(avg_entry_price)
    tick = float(pricetick)
    close = parsed_close
    result.entry_price = entry
    if engine.frozen_entry is None:
        engine.frozen_entry = entry
        engine.frozen_initial_sl = compute_initial_sl(entry, sl_ticks, tick, side)
        engine.frozen_fixed_tp = compute_fixed_tp(entry, tp_ticks, tick, side)
        result.log_entry = not engine.logged_entry
        engine.logged_entry = True

    initial_sl = engine.frozen_initial_sl
    fixed_tp = engine.frozen_fixed_tp
    result.initial_sl = float(initial_sl) if initial_sl is not None else 0.0
    result.tp_price = float(fixed_tp) if fixed_tp is not None else 0.0

    phase = engine.classify_bar(bar_id)
    result.phase = phase
    allow_chandelier_update = phase in (PHASE_FIRST_FULL_BAR, PHASE_NORMAL)

    if allow_chandelier_update:
        high = parse_close_price(high_price)
        low = parse_close_price(low_price)
        if side > 0 and high is not None:
            if engine.highest_high_since_entry is None:
                engine.highest_high_since_entry = high
            else:
                engine.highest_high_since_entry = max(engine.highest_high_since_entry, high)
        if side < 0 and low is not None:
            if engine.lowest_low_since_entry is None:
                engine.lowest_low_since_entry = low
            else:
                engine.lowest_low_since_entry = min(engine.lowest_low_since_entry, low)

    result.highest_high = float(engine.highest_high_since_entry or 0.0)
    result.lowest_low = float(engine.lowest_low_since_entry or 0.0)

    raw = None
    clamped = False
    if chandelier_on and allow_chandelier_update:
        if atr is None or float(atr) <= 0:
            result.chandelier_unavailable = True
        else:
            result.atr = float(atr)
            raw = compute_raw_chandelier(
                side=side,
                highest_high=engine.highest_high_since_entry,
                lowest_low=engine.lowest_low_since_entry,
                atr=float(atr),
                multiplier=float(atr_multiplier),
                pricetick=tick,
            )
            new_dyn, clamped = apply_monotonic_dynamic_stop(side, engine.dynamic_stop, raw)
            if new_dyn != engine.dynamic_stop or phase == PHASE_FIRST_FULL_BAR:
                result.log_chandelier = True
            engine.dynamic_stop = new_dyn
            result.raw_stop = float(raw) if raw is not None else 0.0
            result.monotonic_clamp = clamped
    elif chandelier_on and not allow_chandelier_update:
        result.chandelier_unavailable = True

    dynamic = engine.dynamic_stop
    result.dynamic_stop = float(dynamic) if dynamic is not None else 0.0

    use_dynamic = (
        chandelier_on
        and allow_chandelier_update
        and not result.chandelier_unavailable
        and chandelier_may_trigger(mode or MODE_IMMEDIATE, side, entry, dynamic)
    )

    effective = None
    source = ""
    if initial_sl is not None and use_dynamic and dynamic is not None:
        if side > 0:
            if float(dynamic) > float(initial_sl):
                effective, source = float(dynamic), SOURCE_CHANDELIER
            else:
                effective, source = float(initial_sl), SOURCE_INITIAL_SL
        else:
            if float(dynamic) < float(initial_sl):
                effective, source = float(dynamic), SOURCE_CHANDELIER
            else:
                effective, source = float(initial_sl), SOURCE_INITIAL_SL
    elif use_dynamic and dynamic is not None:
        effective, source = float(dynamic), SOURCE_CHANDELIER
    elif initial_sl is not None:
        effective, source = float(initial_sl), SOURCE_INITIAL_SL

    result.effective_stop = float(effective) if effective is not None else 0.0
    result.sl_price = result.effective_stop
    if not result.triggered:
        result.source = source
    engine.mark_phase_done(phase, bar_id)

    if fixed_tp is not None:
        hit_tp = (close >= float(fixed_tp)) if side > 0 else (close <= float(fixed_tp))
        if hit_tp:
            result.triggered = True
            result.reason = ExitReason.TAKE_PROFIT.value
            result.source = SOURCE_FIXED_TP
            result.trigger_price = float(fixed_tp)
            return result
    if effective is not None:
        hit_sl = (close <= float(effective)) if side > 0 else (close >= float(effective))
        if hit_sl:
            result.triggered = True
            result.reason = ExitReason.STOP_LOSS.value
            result.source = source
            result.trigger_price = float(effective)
            return result
    return result


# V2.4.1 测试兼容别名：V2.5 全关时与旧 evaluate 一样不触发
def evaluate_bar_exit(
    *,
    close_price,
    strategy_pos: int,
    target_pos: int,
    tracked_volume: int,
    avg_entry_price: Optional[float],
    pricetick: Optional[float],
    take_profit_ticks: int = 0,
    stop_loss_ticks: int = 0,
    system_allows_trade: bool = True,
    has_active_orders: bool = False,
    **_unused,
) -> BarExitResult:
    """保留给误 import 的旧测试；V2.5 主路径走 evaluate_v25_exit。"""
    engine = ExitEngine()
    if int(strategy_pos) != 0:
        engine.note_new_position(1 if strategy_pos > 0 else -1, ("x",))
        engine.seen_first_full_bar = True
        engine.seen_entry_bar = True
    return evaluate_v25_exit(
        close_price=close_price,
        high_price=close_price,
        low_price=close_price,
        bar_id=("compat",),
        strategy_pos=strategy_pos,
        target_pos=target_pos,
        tracked_volume=tracked_volume,
        avg_entry_price=avg_entry_price,
        pricetick=pricetick,
        atr=None,
        fixed_tp_ticks=int(take_profit_ticks or 0),
        initial_sl_ticks=int(stop_loss_ticks or 0),
        chandelier_enabled=False,
        atr_multiplier=3.0,
        dynamic_stop_mode=MODE_IMMEDIATE,
        system_allows_trade=system_allows_trade,
        has_active_orders=has_active_orders,
        engine=engine,
    )

try:
    import wqk_ctp_gateway  # 连接 CTP 前 import 才会替换官方类；启动时仍会 attach 现有实例
except Exception:
    wqk_ctp_gateway = None


def _safe_event_names():
    """读取 VeighNa 事件名；导入失败时退回字符串字面量，避免回测环境报错。"""
    try:
        from vnpy.trader.event import EVENT_POSITION, EVENT_TIMER
        return EVENT_TIMER, EVENT_POSITION
    except Exception:
        return "eTimer", "ePosition."


class DoubleMaStrategyV26(CtaTemplate):
    """
    双均线期货实盘策略 V2.6。
    入场：ADX/DMI Trend Filter + Double MA 真交叉。
    出场：沿用 V2.5 Exit Engine。ExecutionCore 仍为 2.3.1，不改。
    """
    author = "DoubleMaStrategyV26"

    # ---------- 可调参数（会显示在 VeighNa 策略面板） ----------
    # 金叉且 Trend=LONG：目标仓 = +fixed_size；死叉且 Trend=SHORT：目标仓 = -fixed_size。
    fast_window: int = 10          # 快均线 SMA 周期（根数）
    slow_window: int = 20          # 慢均线 SMA 周期，必须大于 fast_window
    fixed_size: int = 1            # 每次开仓手数；目标净仓只能是 +N 或 -N

    safety_fuse: bool = True       #安全熔断 True=未就绪/对账失败禁止发新单。False 会绕过闸门，实盘不要关 
    adopt_account_on_mismatch: bool = False  # True=启动时策略仓≠账户仓则直接采用账户仓并放行
    skip_sync_in_backtest: bool = False    # 回测：True没有真实账户，必须跳过 CTP 查询；实盘：False 有真实账户，必须查询

    price_offset: float = 0.0      # 下单价偏移（价格单位）。买/平空加在卖一，卖/平多减在买一
    reconcile_every_bars: int = 1  # 每隔几根 K 线主动查一次账户（持仓事件的兜底）。0=关闭
    fixed_tp_ticks: int = 0        # 固定止盈跳数；0=关闭
    initial_sl_ticks: int = 10     # 初始止损跳数；0=关闭。默认 10
    chandelier_enabled: bool = True  # True 才启用 Chandelier
    atr_period: int = 22           # ATR 周期
    atr_multiplier: float = 2.5    # ATR 倍数，必须 > 0
    dynamic_stop_mode: str = MODE_IMMEDIATE  # IMMEDIATE / PROFIT_ONLY
    adx_period: int = 14
    adx_threshold: float = 30
    trend_confirmation_bars: int = 2

    parameters = [
        "fast_window",
        "slow_window",
        "fixed_size",
        "safety_fuse", #安全熔断
        "adopt_account_on_mismatch",
        "skip_sync_in_backtest",
        "price_offset",
        "reconcile_every_bars",
        "fixed_tp_ticks",
        "initial_sl_ticks",
        "chandelier_enabled",
        "atr_period",
        "atr_multiplier",
        "dynamic_stop_mode",
        "adx_period",
        "adx_threshold",
        "trend_confirmation_bars",
    ]

    # ---------- 运行时变量（面板只读，由 _publish_runtime 从 core 同步） ----------
    # 均线用两根：本根与上一根比较，才能判断「刚刚交叉」而不是「已经在上方」。
    fast_ma0: float = 0.0          # 最新一根快均线
    fast_ma1: float = 0.0          # 上一根快均线
    slow_ma0: float = 0.0          # 最新一根慢均线
    slow_ma1: float = 0.0          # 上一根慢均线

    strategy_pos: int = 0          # 策略内部净仓（多正空负），随成交回报更新
    target_pos: int = 0            # 目标净仓；金叉=+fixed_size，死叉=-fixed_size
    account_pos: int = 0           # 账户净仓，对账用，不直接驱动下单

    trading_state: str = TradingState.IDLE.value       # 开平/反转子状态，例如 CLOSING_LONG
    system_state: str = SystemState.STARTING.value     # 系统闸门：只有 READY/TRADING 才允许交易
    recovery_reason: str = RecoveryReason.NONE.value   # 进入恢复/错误的原因，便于面板排查
    td_ok: bool = True             # 交易通道（下单）是否连通
    md_ok: bool = True             # 行情通道（Tick）是否连通
    exchange_name: str = ""        # SHFE/INE 要拆平今平昨，其余交易所整笔；由 _refresh_exchange() 赋值
    long_today: int = 0            # 今多（当日开的多仓）
    long_yesterday: int = 0        # 昨多（隔夜留下来的多仓）
    short_today: int = 0           # 今空
    short_yesterday: int = 0       # 昨空
    hanging_order: str = ""        # 启动挂接的活动单，例如 O123 CLOSE_LONG 1/2

    exit_reason: str = ExitReason.NONE.value  # 止盈/止损原因；均线交叉不写入
    avg_entry_price: float = 0.0   # Tracker 均价；0 表示无有效入场价
    tracked_volume: int = 0        # Tracker 校验手数，不是仓位权威
    take_profit_price: float = 0.0  # Fixed TP
    stop_loss_price: float = 0.0  # Effective Stop
    initial_sl_price: float = 0.0
    dynamic_stop_price: float = 0.0
    atr_value: float = 0.0
    high_since_entry: float = 0.0
    low_since_entry: float = 0.0
    exit_source: str = ""
    trend_state: str = TREND_UNAVAILABLE
    adx_value: float = 0.0
    plus_di: float = 0.0
    minus_di: float = 0.0
    trend_confirm: str = "0/2"
    # TIMER 约 1 秒一次；内存空单连续空闲这么多次后才按无挂单放行
    ORDER_STREAM_IDLE_POLLS = 3 # 空单连续空闲这么多次后才按无挂单放行
    ORDER_QUERY_WAIT_POLLS = 10 # 查询活动单等待这么多次后才按无挂单放行

    variables = [
        "fast_ma0",
        "fast_ma1",
        "slow_ma0",
        "slow_ma1",
        "strategy_pos",
        "target_pos",
        "account_pos",
        "trading_state",
        "system_state",
        "recovery_reason",
        "td_ok",
        "md_ok",
        "exchange_name",
        "long_today",
        "long_yesterday",
        "short_today",
        "short_yesterday",
        "hanging_order",
        "exit_reason",
        "avg_entry_price",
        "tracked_volume",
        "take_profit_price",
        "stop_loss_price",
        "initial_sl_price",
        "dynamic_stop_price",
        "atr_value",
        "high_since_entry",
        "low_since_entry",
        "exit_source",
        "trend_state",
        "adx_value",
        "plus_di",
        "minus_di",
        "trend_confirm",
    ]

    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        """
        在 VeighNa 界面中填写表单并点击“添加策略”时调用，初始化策略实例
        """
        # BarGenerator：Tick 合成 1 分钟 K 线后回调 on_bar（本策略信号只在分钟线上算）
        self.bg = BarGenerator(self.on_bar)
        self.am = ArrayManager(
            array_manager_size(
                self.fast_window,
                self.slow_window,
                self.atr_period,
                self.adx_period,
            )
        )
        self.last_tick: Optional[TickData] = None  # 最新盘口，用来取买一/卖一下单
        self.last_bar: Optional[BarData] = None    # 最新 K 线，成交跟单时当价格兜底

        # 真正的下单决策、今昨拆腿、对账都在 core 里；本类只负责发单和查账户
        self.core = ExecutionCore()
        self.core.safety_fuse = self.safety_fuse
        self.core.adopt_account_on_mismatch = self.adopt_account_on_mismatch
        self.core.reconcile_every_bars = int(self.reconcile_every_bars)
        self.core.vt_symbol = str(vt_symbol or "")

        self._event_engine = None # 事件引擎    
        self._event_handlers = []  # 事件处理函数列表 [(事件名, 回调)]，停止时按此列表反注册，避免泄漏
        # 启动/重连时 MainEngine 内存为空 ≠ 柜台无挂单，先等私有流回放或 CTP 查询
        self._awaiting_order_stream = False
        self._order_stream_idle_count = 0
        self._awaiting_order_query = False
        self._order_query_wait_count = 0
        self.entry_tracker = EntryPriceTracker()
        self.exit_engine = ExitEngine()
        self._logged_invalid_ticks = False
        self._logged_missing_pricetick = False
        self._last_tpsl_log_key = ""
        self._logged_invalid_mode = False
        self._logged_invalid_atr = False
        self.trend_filter = TrendFilter()
        self._logged_invalid_trend_config = False

    # ==========================================================
    # 生命周期
    # ==========================================================

    def on_init(self):
        """
        VeighNa 初始化（点「初始化」时调用，尚未开始交易）。
        只启动核心并预热均线；真正查账户要等到 on_start。
        """
        self.write_log(f"DoubleMaStrategy V2.6 初始化 ExecutionCore {self.core.version}")
        self.core.start()
        self._publish_runtime()  # 把 core 状态刷到面板变量
        self.load_bar(
            array_manager_size(
                self.fast_window,
                self.slow_window,
                self.atr_period,
                self.adx_period,
            ),
            use_database=True,
        )

    def on_start(self):
        """
        实盘启动（点「启动」）。顺序不能乱：
        1. 把面板上可能改过的参数写回 core
        2. 挂上成交账本（重启后仍能认出已处理成交）
        3. 识别交易所（决定平仓是否拆今昨）
        4. 订阅 TIMER/POSITION
        5. 两阶段同步账户仓 + 活动单，通过后才允许交易
        """
        self.write_log("DoubleMaStrategy V2.6 启动")
        self._warn_if_invalid_ticks()
        # 面板参数可能在初始化后被改过，启动时再同步一遍
        self.core.safety_fuse = self.safety_fuse
        self.core.adopt_account_on_mismatch = self.adopt_account_on_mismatch
        self.core.reconcile_every_bars = int(self.reconcile_every_bars)
        self.core.strategy_pos = int(self.strategy_pos)
        self.core.vt_symbol = str(self.vt_symbol or "")
        self.core.start()
        self._attach_trade_ledger()
        self._refresh_exchange() # 识别交易所（决定平仓是否拆今昨）
        self._register_engine_events() # 订阅 TIMER/POSITION
        self._sync_from_account("策略启动") # 两阶段同步:账户仓+活动单，通过后才允许交易
        self._poll_gateway() # 探测交易/行情通道是否还连着/“主动轮询”模式（K线对账兜底）
        self._publish_runtime()
        self.write_log(self._format_trend_log())
        self.put_event()  # 通知界面刷新

    def on_stop(self):
        """停止：先退订事件再撤单，避免回调在停止过程中又发出新单。"""
        self.write_log("DoubleMaStrategy V2.6 停止")
        self._unregister_engine_events()
        self.cancel_all()
        self.core.stop()
        self._publish_runtime()
        self.put_event()

    def _attach_trade_ledger(self) -> None:
        """实盘挂 SQLite 成交账本；回测只走内存，避免写盘。"""
        if self.skip_sync_in_backtest:
            return
        path = default_trade_ledger_path(self.strategy_name, self.vt_symbol, _THIS_DIR)
        self.core.attach_trade_ledger(path)
        self.write_log(self.core.last_message)

    # ==========================================================
    # Gateway / 事件
    # ==========================================================

    def _register_engine_events(self) -> None:
        """
        订阅两类事件（回测默认跳过，因为没有真实 Gateway）：
        - TIMER：周期性探测交易/行情通道是否还连着/“主动轮询”模式（K线对账兜底）
        - POSITION：账户持仓一变就对账，不必等下一根 K 线/“被动轮询”模式（持仓事件兜底）
        """
        self._unregister_engine_events()
        if self._is_backtesting() and self.skip_sync_in_backtest:
            return

        event_engine = self._get_event_engine()
        if event_engine is None or not hasattr(event_engine, "register"):
            # 找不到事件引擎也不致命：on_bar 里仍会轮询网关并按 K 线对账
            self.write_log("未找到 EventEngine，将仅用 K 线兜底对账和断线检测")
            return

        timer_name, position_name = _safe_event_names()

        def on_timer(_event):
            try:
                self._poll_gateway()
            except Exception as exc:
                self.write_log(f"Gateway 轮询异常：{exc}")

        def on_position(_event):
            try:
                self._reconcile_account("EVENT_POSITION")
            except Exception as exc:
                self.write_log(f"持仓事件对账异常：{exc}")

        event_engine.register(timer_name, on_timer)
        event_engine.register(position_name, on_position)
        self._event_engine = event_engine
        self._event_handlers = [
            (timer_name, on_timer),
            (position_name, on_position),
        ]
        self.write_log("已订阅 EVENT_TIMER / EVENT_POSITION")

    def _unregister_engine_events(self) -> None:
        """停止时按注册列表退订，避免残留回调继续改仓。"""
        if not self._event_engine:
            self._event_handlers = []
            return
        for name, handler in self._event_handlers:
            unregister = getattr(self._event_engine, "unregister", None)
            if unregister:
                try:
                    unregister(name, handler)
                except Exception:
                    pass
        self._event_engine = None
        self._event_handlers = []

    def _get_event_engine(self):
        """优先走 MainEngine.event_engine；回测引擎可能没有 main_engine。"""
        main_engine = getattr(self.cta_engine, "main_engine", None)
        if main_engine is not None:
            engine = getattr(main_engine, "event_engine", None)
            if engine is not None:
                return engine
        return getattr(self.cta_engine, "event_engine", None)

    def _poll_gateway(self) -> None:
        """
        读取通道状态并交给核心。
        lost：立刻禁单；restored：必须立刻两阶段同步，不能直接恢复交易。
        flags 为 None 表示环境无法判断（回测/无网关），故意不动作，避免误判断线。
        """
        flags = self._read_gateway_flags()
        if flags is None:
            # 读不到通道（如 PAPER 无 login_status）不当成断线，但等订单流仍要计数，否则永远卡在 SYNCING_ORDERS
            if self._awaiting_order_query:
                self._retry_ctp_order_query()
            elif self._awaiting_order_stream:
                if self._order_stream_idle_count == 0:
                    self.write_log(
                        "未读到 Gateway 登录状态，按空闲轮询继续订单对账，不当成断线"
                    )
                self._retry_order_sync(count_idle=True)
            return
        td_ok, md_ok = flags
        action = self.core.on_gateway_status(td_ok, md_ok)
        self._publish_runtime()
        if action == "lost":
            self.write_log(self.core.last_message)
            self.put_event()
        elif action == "restored":
            self.write_log(self.core.last_message)
            self._sync_from_account("Gateway 重连")
            self.put_event()
        elif self._awaiting_order_query:
            self._retry_ctp_order_query()
        elif self._awaiting_order_stream:
            self._retry_order_sync(count_idle=td_ok)

    def _read_gateway_flags(self) -> Optional[Tuple[bool, bool]]:
        """
        读取本策略合约所属 Gateway 的 TD/MD 状态。
        多网关不做 OR：只认合约 gateway_name，认不出则返回 None。
        无法判断时返回 None，避免回测/无网关环境被误判为断线。
        """
        main_engine = getattr(self.cta_engine, "main_engine", None)
        if main_engine is None:
            return None

        gateway = self._resolve_strategy_gateway(main_engine)
        if gateway is None:
            return None

        td, md, found = self._gateway_channel_flags(gateway)
        if not found:
            return None
        return td, md

    def _resolve_strategy_gateway(self, main_engine):
        """只取本合约那个 Gateway。多个且无法对应时返回 None，禁止用其它通道冒充。"""
        wanted = ""
        getter = getattr(main_engine, "get_contract", None)
        if getter:
            try:
                contract = getter(self.vt_symbol)
                wanted = str(getattr(contract, "gateway_name", "") or "")
            except Exception:
                wanted = ""

        if wanted:
            gateway = self._lookup_gateway(main_engine, wanted)
            if gateway is not None:
                return gateway

        gateways = self._list_gateways(main_engine)
        if wanted:
            for gateway in gateways:
                if str(getattr(gateway, "gateway_name", "") or "") == wanted:
                    return gateway
            return None
        if len(gateways) == 1:
            return gateways[0]
        return None

    @staticmethod
    def _lookup_gateway(main_engine, name: str):
        """静默查找，不用 MainEngine.get_gateway（找不到会每秒刷「找不到底层接口」）。"""
        if not name:
            return None
        mapping = getattr(main_engine, "gateways", None)
        if isinstance(mapping, dict):
            return mapping.get(name)
        return None

    @staticmethod
    def _list_gateways(main_engine) -> list:
        mapping = getattr(main_engine, "gateways", None)
        if isinstance(mapping, dict):
            return [item for item in mapping.values() if item is not None]
        return []

    def _gateway_channel_flags(self, gateway) -> Tuple[bool, bool, bool]:
        """
        从单个 Gateway 解析 (交易通, 行情通, 是否读到状态)。
        官方 CtpGateway 没有 td_connected / status，优先读 td_api/md_api.login_status。
        """
        td_api = getattr(gateway, "td_api", None)
        md_api = getattr(gateway, "md_api", None)
        td_login = getattr(td_api, "login_status", None) if td_api is not None else None
        md_login = getattr(md_api, "login_status", None) if md_api is not None else None
        if td_login is not None or md_login is not None:
            return bool(td_login), bool(md_login), True

        status = getattr(gateway, "status", None)
        if isinstance(status, dict) and status:
            td = self._flag_from_mapping(status, ("td", "trade", "trader", "交易"))
            md = self._flag_from_mapping(status, ("md", "market", "行情"))
            if td is None and md is None:
                values = [self._as_bool(v) for v in status.values()]
                connected = all(values) if values else False
                return connected, connected, True
            return bool(td), bool(md), True

        td = getattr(gateway, "td_connected", None)
        md = getattr(gateway, "md_connected", None)
        if td is None:
            td = getattr(gateway, "td_status", None)
        if md is None:
            md = getattr(gateway, "md_status", None)
        if td is not None or md is not None:
            return bool(td), bool(md), True

        for attr in ("connected", "connect_status", "login_status"):
            value = getattr(gateway, attr, None)
            if value is not None and not callable(value):
                flag = self._as_bool(value)
                return flag, flag, True

        name = f"{getattr(gateway, 'gateway_name', '')} {type(gateway).__name__}".lower()
        if "paper" in name:
            # 模拟盘 Paper 网关通常没有 td_api.login_status，有实例即视为通道可用
            return True, True, True
        return False, False, False

    @staticmethod
    def _flag_from_mapping(status: dict, keys: Tuple[str, ...]) -> Optional[bool]:
        """在 status 字典里用关键字模糊匹配交易/行情通道。"""
        for raw_key, value in status.items():
            key = str(raw_key).lower()
            if any(token in key for token in keys):
                if hasattr(value, "value"):
                    return bool(value.value)
                return bool(value)
        return None

    @staticmethod
    def _as_bool(value) -> bool:
        if hasattr(value, "value"):
            return bool(value.value)
        return bool(value)

    # ==========================================================
    # 账户同步 / 周期对账
    # ==========================================================

    def _sync_from_account(self, source: str) -> None:
        """
        启动/重连时的两阶段同步，必须按顺序：
        1. 查账户持仓 → sync_position
        2. 查活动委托 → sync_orders
        对账失败进入 RECOVERING，自动交易暂停。
        恢复办法：确认账户无误后把 adopt_account_on_mismatch 设 True 并重启，
        或在引擎里调用 confirm_adopt_account_position()。
        """
        self.write_log(f"持仓同步开始：{source}")
        self._refresh_exchange()

        if self._is_backtesting() and self.skip_sync_in_backtest:
            # 回测没有真实账户：策略仓就是账户仓，直接 READY
            self.core.ready_from_backtest(int(self.pos))
            self._publish_runtime()
            self.write_log(self.core.last_message)
            self._invalidate_tracker_if_desynced(source)
            return

        pos_ok, snapshot = self.query_account_position() # 查询账户持仓
        if not self.core.sync_position(snapshot, query_ok=pos_ok): # 同步账户持仓
            # 持仓都查不到，后面订单对账没有意义，直接停在 ERROR
            self._publish_runtime()
            self.write_log(self.core.last_message)
            self._invalidate_tracker_if_desynced(source)
            return

        order_source, orders = self._read_engine_active_orders()
        if order_source == "failed":
            self.core.sync_orders([], query_ok=False)
        elif order_source == "engine_empty":
            if self._try_start_ctp_order_query():
                self._awaiting_order_query = True
                self._order_query_wait_count = 0
                self._awaiting_order_stream = False
                self.write_log("已向柜台查询委托，等待回报后再对账。")
            else:
                # PAPER / 无 query_order：本进程内存为空不是柜台确认
                self._awaiting_order_stream = True
                self._order_stream_idle_count = 0
                self.write_log(
                    "活动单仅来自本进程内存且为空，尚未向柜台确认，暂不放行。"
                    "等待私有流回放后再对账。"
                )
        else:
            self._clear_order_stream_wait()
            self.core.sync_orders(orders, query_ok=True)

        if self.core.system_state == SystemState.RECOVERING:
            self.write_log(
                "进入恢复模式，自动交易已暂停。"
                "若有挂单：等待成交，或调用 cancel_hanging_orders() 撤单后再 "
                "confirm_adopt_account_position()。"
            )

        self._publish_runtime()
        self.write_log(
            f"持仓同步结束：system={self.core.system_state.value}, "
            f"reason={self.core.recovery_reason.value}, "
            f"strategy_pos={self.core.strategy_pos}, "
            f"account_pos={self.core.account_pos}, "
            f"msg={self.core.last_message}"
        )
        self._invalidate_tracker_if_desynced(source)

    def _reconcile_account(self, source: str) -> None:
        """
        运行中对账（持仓事件或 K 线兜底）。
        只刷新 account_pos：不一致则暂停，一致则从 RECOVERING 放行。
        故意不改 target_pos，避免对账把正在执行的信号目标冲掉。
        """
        if self._is_backtesting() and self.skip_sync_in_backtest:
            return
        pos_ok, snapshot = self.query_account_position()
        result = self.core.apply_position_update(snapshot, query_ok=pos_ok)
        if result.action in ("mismatch", "recovered"):
            self.write_log(f"{source}：{result.reason}")
        self._publish_runtime()

    def confirm_adopt_account_position(self) -> None:
        """
        人工确认入口：以当前账户持仓覆盖策略仓，并重新允许交易。
        仍要求能查到持仓和活动单；有挂单时核心会拒绝确认。
        """
        pos_ok, snapshot = self.query_account_position()
        if not pos_ok:
            self.core.recovery.enter_error("人工确认失败：无法查询账户持仓")
            self._publish_runtime()
            return
        self.core.sync_position(snapshot, query_ok=True)
        order_source, orders = self._read_engine_active_orders()
        if order_source == "failed":
            self.core.recovery.enter_error("人工确认失败：无法查询活动订单")
            self._publish_runtime()
            return
        # 人工确认：内存空也按当前可见结果放行
        self._clear_order_stream_wait()
        self.core.sync_orders(orders, query_ok=True)
        adopted = self.core.adopt_account_position()
        self._publish_runtime()
        self.write_log(self.core.last_message)
        if adopted:
            self._invalidate_tracker_if_desynced("adopt")

    def cancel_hanging_orders(self) -> None:
        """恢复模式下撤掉启动挂接的活动单，撤完后再 adopt。"""
        self.write_log("撤启动挂单")
        self.cancel_all()
        self._publish_runtime()

    def query_account_position(self) -> Tuple[bool, PositionSnapshot]:
        """
        从 MainEngine 汇总本合约多空持仓。
        CTP 的 yd_volume 是昨仓；今仓 = 总量 - 昨仓。无引擎时退回 self.pos。
        """
        try:
            main_engine = getattr(self.cta_engine, "main_engine", None) # 获取主引擎
            getter = getattr(main_engine, "get_all_positions", None) if main_engine else None # 获取所有持仓
            if getter is None:
                # 回测/无 MainEngine：只能用 CTA 引擎自己的 self.pos，全部记为今仓
                return True, PositionSnapshot.from_net(int(self.pos))
            
            #初始化四个累加器
            long_vol = short_vol = 0 
            long_yd = short_yd = 0
            for pos in getter() or []: #getter() 返回一个持仓列表（List[PositionData]），遍历每个持仓
                if getattr(pos, "vt_symbol", "") != self.vt_symbol:
                    continue
                volume = int(getattr(pos, "volume", 0) or 0)
                yd_volume = int(getattr(pos, "yd_volume", 0) or 0)  # CTP 昨仓
                direction = self._enum_name(getattr(pos, "direction", ""))
                if direction == "SHORT":
                    short_vol += volume
                    short_yd += yd_volume
                else:
                    long_vol += volume
                    long_yd += yd_volume
            return True, PositionSnapshot(
                long_volume=long_vol,
                short_volume=short_vol,
                # 今仓 = 总量 - 昨仓；max 防止柜台数据异常出现负数
                long_today=max(0, long_vol - long_yd),
                long_yesterday=long_yd,
                short_today=max(0, short_vol - short_yd),
                short_yesterday=short_yd,
            )
        except Exception as exc:
            self.write_log(f"查询账户持仓失败：{exc}")
            return False, PositionSnapshot()

    def query_active_orders(self) -> Tuple[bool, List[OrderSnapshot]]:
        """
        查询本合约未完成委托。
        query_ok=True 只表示拿到了可用列表（内存里有单，或回测退回 core）。
        有 MainEngine 但内存为空时 query_ok=False：不能当成柜台确认无挂单。
        """
        source, orders = self._read_engine_active_orders()
        if source in ("engine", "core"):
            return True, orders
        return False, orders

    def _read_engine_active_orders(self) -> Tuple[str, List[OrderSnapshot]]:
        """
        读本进程订单缓存。
        engine=内存有活动单；engine_empty=有引擎但缓存为空；
        core=无 MainEngine，退回核心登记；failed=查询异常。
        """
        try:
            collected: List[OrderSnapshot] = []
            main_engine = getattr(self.cta_engine, "main_engine", None)
            getter = getattr(main_engine, "get_all_active_orders", None) if main_engine else None
            if getter:
                for order in getter() or []:
                    if getattr(order, "vt_symbol", "") != self.vt_symbol:
                        continue
                    snapshot = self._to_order_snapshot(order)
                    if snapshot.active:
                        collected.append(snapshot)
                if collected:
                    return "engine", collected
                return "engine_empty", []
            collected.extend(self.core.active_orders.values())
            return "core", collected
        except Exception as exc:
            self.write_log(f"查询活动订单失败：{exc}")
            return "failed", []

    def _retry_order_sync(self, count_idle: bool) -> None:
        """等私有流回放：出现挂单立刻挂接；允许计数且连续空闲则按无挂单放行。"""
        if not self._awaiting_order_stream:
            return
        source, orders = self._read_engine_active_orders()
        if source == "engine":
            self._clear_order_stream_wait()
            self.core.sync_orders(orders, query_ok=True)
            self.write_log(self.core.last_message)
            self._publish_runtime()
            self.put_event()
            return
        if source == "engine_empty" and count_idle:
            self._order_stream_idle_count += 1
            if self._order_stream_idle_count >= int(self.ORDER_STREAM_IDLE_POLLS):
                self._clear_order_stream_wait()
                self.core.sync_orders([], query_ok=True)
                self.write_log(
                    "私有流空闲后按本进程无挂单放行（非柜台 query_order）"
                )
                self._publish_runtime()
                self.put_event()

    def _clear_order_stream_wait(self) -> None:
        self._awaiting_order_stream = False
        self._order_stream_idle_count = 0
        self._awaiting_order_query = False
        self._order_query_wait_count = 0

    def _ctp_query_gateway(self):
        main_engine = getattr(self.cta_engine, "main_engine", None)
        if main_engine is None or wqk_ctp_gateway is None:
            return None
        gateway = self._resolve_strategy_gateway(main_engine)
        if gateway is None:
            return None
        name = f"{getattr(gateway, 'gateway_name', '')} {type(gateway).__name__}".lower()
        if "paper" in name:
            return None
        return gateway

    def _try_start_ctp_order_query(self) -> bool:
        gateway = self._ctp_query_gateway()
        if gateway is None:
            return False
        try:
            wqk_ctp_gateway.attach_wqk_ctp(gateway)
            return bool(wqk_ctp_gateway.request_order_query(gateway))
        except Exception as exc:
            self.write_log(f"柜台委托查询发起失败：{exc}")
            return False

    def _retry_ctp_order_query(self) -> None:
        if not self._awaiting_order_query:
            return
        source, orders = self._read_engine_active_orders()
        if source == "engine":
            self._clear_order_stream_wait()
            self.core.sync_orders(orders, query_ok=True)
            self.write_log(self.core.last_message)
            self._publish_runtime()
            self.put_event()
            return
        gateway = self._ctp_query_gateway()
        finished = bool(gateway) and wqk_ctp_gateway.order_query_finished(gateway)
        self._order_query_wait_count += 1
        if finished:
            self._clear_order_stream_wait()
            self.core.sync_orders(orders, query_ok=True)
            self.write_log("柜台委托查询完成，按查询结果对账")
            self._publish_runtime()
            self.put_event()
            return
        if self._order_query_wait_count >= int(self.ORDER_QUERY_WAIT_POLLS):
            self._awaiting_order_query = False
            self._awaiting_order_stream = True
            self._order_stream_idle_count = 0
            self.write_log("柜台委托查询超时，退回本进程内存对账")

    def _is_backtesting(self) -> bool:
        """根据引擎类型判断是否回测，避免回测里去查真实账户。"""
        try:
            engine_type = self.get_engine_type()
            text = f"{getattr(engine_type, 'name', '')} {getattr(engine_type, 'value', '')} {engine_type}"
            return "BACKTEST" in text.upper() or "回测" in text
        except Exception:
            return False

    def _refresh_exchange(self) -> None:
        """识别交易所（SHFE/INE 要拆平今平昨，其余用整笔 CLOSE）。"""
        raw = ""
        main_engine = getattr(self.cta_engine, "main_engine", None)
        getter = getattr(main_engine, "get_contract", None) if main_engine else None
        if getter:
            try:
                contract = getter(self.vt_symbol)
            except Exception:
                contract = None
            if contract is not None:
                raw = getattr(contract, "exchange", "") or ""
        self.core.set_exchange(raw or self.vt_symbol)
        self.exchange_name = self.core.exchange

    @staticmethod
    def _to_vnpy_offset(offset_kind: OffsetKind) -> Offset:
        """把核心的 OffsetKind 转成 VeighNa Offset；部分环境没有 CLOSEYESTERDAY。"""
        if offset_kind == OffsetKind.OPEN:
            return Offset.OPEN
        if offset_kind == OffsetKind.CLOSETODAY:
            return Offset.CLOSETODAY
        if offset_kind == OffsetKind.CLOSEYESTERDAY:
            # 部分 VeighNa 版本没有 CLOSEYESTERDAY，上期所平昨用 CLOSE 即可
            return getattr(Offset, "CLOSEYESTERDAY", Offset.CLOSE)
        return Offset.CLOSE  # 默认平仓（大商/郑商/中金/广期，以及上期所平昨）

    @staticmethod
    def _enum_name(value) -> str:
        """Exchange.SHFE / 'Direction.LONG' 等统一抽成纯名字。"""
        name = getattr(value, "name", None)
        if name:
            return str(name)
        text = str(value)
        return text.split(".")[-1] if "." in text else text

    def _to_order_snapshot(self, order: OrderData) -> OrderSnapshot:
        """把 VeighNa OrderData 转成核心用的快照；意图优先沿用已登记记录。"""
        status_name = self._enum_name(getattr(order, "status", ""))
        status = (
            OrderStatus[status_name]
            if status_name in OrderStatus.__members__
            else OrderStatus.UNKNOWN
        )
        known = self.core.active_orders.get(order.vt_orderid) or self.core.order_history.get(
            order.vt_orderid
        )
        direction = infer_direction_kind(getattr(order, "direction", ""))
        offset = infer_offset_kind(getattr(order, "offset", ""))
        intent = known.intent if known else OrderIntent.UNKNOWN
        if intent == OrderIntent.UNKNOWN:
            intent = infer_order_intent(direction, offset)

        if hasattr(order, "is_active") and order.is_active() and status == OrderStatus.UNKNOWN:
            # 状态名对不上枚举、但订单仍活动：按未成交处理，避免漏记挂单
            status = OrderStatus.NOTTRADED

        return OrderSnapshot(
            vt_orderid=str(order.vt_orderid),
            intent=intent,
            volume=int(getattr(order, "volume", 0) or 0),
            traded=int(getattr(order, "traded", 0) or 0),
            status=status,
            offset=offset,
            vt_symbol=str(getattr(order, "vt_symbol", "") or self.vt_symbol or ""),
            direction=direction,
            price=float(getattr(order, "price", 0) or 0),
        )

    def _publish_runtime(self) -> None:
        """把核心状态刷到策略变量，供面板显示和 persist。"""
        self.strategy_pos = self.core.strategy_pos
        self.target_pos = self.core.target_pos
        self.account_pos = self.core.account_pos
        self.trading_state = self.core.trading_state.value
        self.system_state = self.core.system_state.value
        self.recovery_reason = self.core.recovery_reason.value
        self.td_ok = self.core.gateway.td_ok
        self.md_ok = self.core.gateway.md_ok
        self.exchange_name = self.core.exchange
        working = self.core.working_position
        self.long_today = int(working.long_today)
        self.long_yesterday = int(working.long_yesterday)
        self.short_today = int(working.short_today)
        self.short_yesterday = int(working.short_yesterday)
        self.hanging_order = self.core.hanging_order_text
        self.tracked_volume = int(self.entry_tracker.tracked_volume)
        avg = self.entry_tracker.avg_entry_price
        self.avg_entry_price = float(avg) if avg is not None else 0.0
        self.take_profit_price = float(self.exit_engine.frozen_fixed_tp or 0.0)
        self.initial_sl_price = float(self.exit_engine.frozen_initial_sl or 0.0)
        self.dynamic_stop_price = float(self.exit_engine.dynamic_stop or 0.0)
        if self.exit_engine.frozen_initial_sl is not None and self.exit_engine.dynamic_stop is not None:
            pos = int(self.core.strategy_pos)
            if pos > 0:
                self.stop_loss_price = max(
                    self.exit_engine.frozen_initial_sl,
                    self.exit_engine.dynamic_stop,
                )
            elif pos < 0:
                self.stop_loss_price = min(
                    self.exit_engine.frozen_initial_sl,
                    self.exit_engine.dynamic_stop,
                )
            else:
                self.stop_loss_price = 0.0
        elif self.exit_engine.dynamic_stop is not None:
            self.stop_loss_price = float(self.exit_engine.dynamic_stop)
        else:
            self.stop_loss_price = float(self.exit_engine.frozen_initial_sl or 0.0)
        self.high_since_entry = float(self.exit_engine.highest_high_since_entry or 0.0)
        self.low_since_entry = float(self.exit_engine.lowest_low_since_entry or 0.0)
        self.atr_value = float(self._read_atr() or 0.0)
        self.trend_state = str(self.trend_filter.state)
        self.adx_value = float(self.trend_filter.adx or 0.0)
        self.plus_di = float(self.trend_filter.plus_di or 0.0)
        self.minus_di = float(self.trend_filter.minus_di or 0.0)
        needed = max(int(self.trend_confirmation_bars or 0), 1)
        active = int(self.trend_filter.long_confirm or self.trend_filter.short_confirm)
        self.trend_confirm = f"{active}/{needed}"

    def _warn_if_invalid_ticks(self) -> None:
        tp = int(self.fixed_tp_ticks)
        sl = int(self.initial_sl_ticks)
        if tp < 0 or sl < 0:
            if not self._logged_invalid_ticks:
                self.write_log(
                    f"[V2.5] ticks 非法 fixed_tp={tp} initial_sl={sl}，对应功能禁用，不猜距离"
                )
                self._logged_invalid_ticks = True
        if int(self.atr_period) <= 0 or float(self.atr_multiplier) <= 0:
            if not self._logged_invalid_atr:
                self.write_log(
                    f"[V2.5] ATR 参数非法 period={self.atr_period} "
                    f"multiplier={self.atr_multiplier}，Chandelier 禁用"
                )
                self._logged_invalid_atr = True
        if normalize_dynamic_mode(self.dynamic_stop_mode) is None:
            if not self._logged_invalid_mode:
                self.write_log(
                    f"[V2.5] dynamic_stop_mode 非法 {self.dynamic_stop_mode!r}，"
                    f"只允许 IMMEDIATE / PROFIT_ONLY，Chandelier 禁用"
                )
                self._logged_invalid_mode = True

    def _read_pricetick(self) -> Optional[float]:
        """读合约最小变动价；读不到则 TP/SL unavailable，不用 1.0 猜测。"""
        main_engine = getattr(self.cta_engine, "main_engine", None)
        getter = getattr(main_engine, "get_contract", None) if main_engine else None
        if getter is None:
            return None
        try:
            contract = getter(self.vt_symbol)
        except Exception:
            return None
        if contract is None:
            return None
        raw = getattr(contract, "pricetick", None)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        if value <= 0:
            return None
        return value

    def _has_live_orders(self) -> bool:
        return any(order.active for order in self.core.active_orders.values())

    def _read_atr(self) -> Optional[float]:
        try:
            arr = self.am.atr(int(self.atr_period), array=True)
        except Exception:
            return None
        if arr is None or len(arr) == 0:
            return None
        try:
            value = float(arr[-1])
        except (TypeError, ValueError):
            return None
        if value != value or value <= 0:
            return None
        return value

    def _read_dmi(self) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        period = int(self.adx_period or 0)
        if period <= 0:
            return None, None, None
        try:
            adx = parse_indicator_value(self.am.adx(period))
            plus_di = parse_indicator_value(self.am.plus_di(period))
            minus_di = parse_indicator_value(self.am.minus_di(period))
        except Exception:
            return None, None, None
        return adx, plus_di, minus_di

    def _format_trend_log(self, result: Optional[TrendTickResult] = None) -> str:
        filt = self.trend_filter
        needed = max(int(self.trend_confirmation_bars or 0), 1)
        if filt.candidate == CANDIDATE_LONG:
            confirm = f"{filt.long_confirm}/{needed}"
        elif filt.candidate == CANDIDATE_SHORT:
            confirm = f"{filt.short_confirm}/{needed}"
        else:
            confirm = f"0/{needed}"
        adx = result.adx if result is not None else filt.adx
        plus_di = result.plus_di if result is not None else filt.plus_di
        minus_di = result.minus_di if result is not None else filt.minus_di
        return (
            f"[V2.6 TREND] ADX={adx} +DI={plus_di} -DI={minus_di} "
            f"candidate={filt.candidate} confirm={confirm} state={filt.state}"
        )

    def _update_trend_on_bar(self) -> TrendTickResult:
        if invalid_trend_config(self.adx_threshold, self.trend_confirmation_bars):
            if not self._logged_invalid_trend_config:
                self.write_log(
                    f"[V2.6 TREND] 参数非法 adx_threshold={self.adx_threshold} "
                    f"trend_confirmation_bars={self.trend_confirmation_bars}，"
                    f"Trend=UNAVAILABLE，禁止新开仓，不猜默认值"
                )
                self._logged_invalid_trend_config = True
        adx, plus_di, minus_di = self._read_dmi()
        result = update_trend_filter(
            self.trend_filter,
            adx=adx,
            plus_di=plus_di,
            minus_di=minus_di,
            adx_threshold=self.adx_threshold,
            trend_confirmation_bars=self.trend_confirmation_bars,
        )
        if result.invalid_config:
            result.adx = 0.0
            result.plus_di = 0.0
            result.minus_di = 0.0
        if self.trading and result.log_trend:
            self.write_log(self._format_trend_log(result))
        return result

    def _current_entry_bar_id(self, trade=None) -> Optional[Tuple]:
        tick = self.last_tick
        dt = getattr(tick, "datetime", None) if tick is not None else None
        if dt is None and trade is not None:
            dt = getattr(trade, "datetime", None)
        if dt is None and self.last_bar is not None:
            dt = getattr(self.last_bar, "datetime", None)
        return minute_key(dt)

    def _invalidate_tracker_if_desynced(self, source: str) -> None:
        """仅同步/adopt 成功路径调用。手数一致则保留均价（同一实例停开）。"""
        if not maybe_invalidate_tracker(self.entry_tracker, int(self.core.strategy_pos)):
            return
        reason = (
            f"[V2.5 EXIT] unavailable reason=TRACKER_MISMATCH "
            f"invalidate after {source} strategy_pos={self.core.strategy_pos}"
        )
        self.exit_engine.reset()
        self._emit_tpsl_log(True, reason)
        self._publish_runtime()

    def _emit_tpsl_log(self, unavailable: bool, reason: str = "") -> None:
        new_key, message = next_tpsl_log(self._last_tpsl_log_key, unavailable, reason)
        self._last_tpsl_log_key = new_key
        if message:
            self.write_log(message)

    def _try_bar_exit(self, bar: BarData) -> bool:
        """
        仓位稳住后才用收盘价判断。触发则只改 target_pos=0 并 drive。
        返回 True 表示本根已出场，调用方必须 return，不得再处理均线。
        """
        self._warn_if_invalid_ticks()
        pricetick = self._read_pricetick()
        chandelier_on = bool(self.chandelier_enabled)
        if chandelier_on and (
            int(self.atr_period) <= 0
            or float(self.atr_multiplier) <= 0
            or normalize_dynamic_mode(self.dynamic_stop_mode) is None
        ):
            chandelier_on = False
        exits_on = (
            int(self.fixed_tp_ticks) > 0
            or int(self.initial_sl_ticks) > 0
            or chandelier_on
        )
        if exits_on and pricetick is None and not self._logged_missing_pricetick:
            self.write_log("[V2.5 EXIT] unavailable reason=PRICETICK_UNAVAILABLE")
            self._logged_missing_pricetick = True

        atr = self._read_atr() if chandelier_on else None
        self.atr_value = float(atr or 0.0)
        result = evaluate_v25_exit(
            close_price=parse_bar_close_price(bar),
            high_price=getattr(bar, "high_price", None),
            low_price=getattr(bar, "low_price", None),
            bar_id=minute_key(getattr(bar, "datetime", None)),
            strategy_pos=int(self.core.strategy_pos),
            target_pos=int(self.core.target_pos),
            tracked_volume=int(self.entry_tracker.tracked_volume),
            avg_entry_price=self.entry_tracker.avg_entry_price,
            pricetick=pricetick,
            atr=atr,
            fixed_tp_ticks=int(self.fixed_tp_ticks),
            initial_sl_ticks=int(self.initial_sl_ticks),
            chandelier_enabled=chandelier_on,
            atr_multiplier=float(self.atr_multiplier),
            dynamic_stop_mode=str(self.dynamic_stop_mode),
            system_allows_trade=self.core.system_allows_trade(),
            has_active_orders=self._has_live_orders(),
            engine=self.exit_engine,
        )
        if result.log_entry:
            side = "LONG" if self.core.strategy_pos > 0 else "SHORT"
            self.write_log(
                f"[V2.5 ENTRY] side={side} entry={result.entry_price} "
                f"initial_sl={result.initial_sl} fixed_tp={result.tp_price}"
            )
        if result.log_chandelier:
            side = "LONG" if self.core.strategy_pos > 0 else "SHORT"
            clamp = " reason=MONOTONIC_CLAMP" if result.monotonic_clamp else ""
            self.write_log(
                f"[V2.5 CHANDELIER] side={side} "
                f"high_since_entry={result.highest_high} "
                f"low_since_entry={result.lowest_low} "
                f"atr={result.atr} multiplier={self.atr_multiplier} "
                f"raw_stop={result.raw_stop} dynamic_stop={result.dynamic_stop} "
                f"initial_sl={result.initial_sl} "
                f"effective_stop={result.effective_stop}{clamp}"
            )
        if result.unavailable:
            self._emit_tpsl_log(True, result.unavailable_reason)
            return False
        pos = int(self.core.strategy_pos)
        if (
            pos != 0
            and pos == int(self.core.target_pos)
            and pos == int(self.entry_tracker.tracked_volume)
            and self.entry_tracker.avg_entry_price is not None
        ):
            self._emit_tpsl_log(False)
        if not result.triggered:
            self.exit_source = ""
            self._publish_runtime()
            return False

        old_target = int(self.core.target_pos)
        self.exit_reason = result.reason
        self.exit_source = result.source
        self.core.set_target_pos(0)
        bar_time = getattr(bar, "datetime", "")
        direction = "LONG" if self.core.strategy_pos > 0 else "SHORT"
        self.write_log(
            f"[V2.5 EXIT] reason={result.reason} source={result.source} "
            f"symbol={self.vt_symbol} direction={direction} "
            f"volume={self.core.strategy_pos} entry={result.entry_price} "
            f"close={result.close_price} tp={result.tp_price} "
            f"effective_stop={result.effective_stop} "
            f"trigger={result.trigger_price} phase={result.phase} "
            f"bar={bar_time} strategy_pos={self.core.strategy_pos} "
            f"target_pos {old_target} -> {self.core.target_pos} "
            f"account_pos={self.core.account_pos}"
        )
        self.drive_execution(bar)
        return True

    # ==========================================================
    # Tick / Bar
    # ==========================================================

    def on_tick(self, tick: TickData):
        """缓存最新盘口，并交给 BarGenerator 合成 K 线。"""
        self.last_tick = tick
        self.bg.update_tick(tick)

    def on_bar(self, bar: BarData):
        """
        每根 K 线主循环：
        1. note_bar / 网关 / 对账
        2. 更新 AM；未 inited 则整根 return（与 V2.5 相同）
        3. ADX/DMI → Trend State
        4. V2.5 Exit Engine；触发则 drive 后本根 return
        5. 仅 trading 阶段的新金叉/死叉，且方向与 Trend 一致，才改 target
        """
        self.last_bar = bar
        self.core.note_bar()
        self._poll_gateway() # 探测交易/行情通道是否还连着
        if self.core.should_reconcile_on_bar():
            self._reconcile_account("on_bar") # 对账

        self.am.update_bar(bar)
        if not self.am.inited:
            return  # 历史 K 线还不够算慢均线 / ATR / ADX

        fast_ma = self.am.sma(self.fast_window, array=True)
        slow_ma = self.am.sma(self.slow_window, array=True)
        self.fast_ma0 = float(fast_ma[-1])
        self.fast_ma1 = float(fast_ma[-2])
        self.slow_ma0 = float(slow_ma[-1])
        self.slow_ma1 = float(slow_ma[-2])

        self._update_trend_on_bar()
        self._publish_runtime()

        if self._try_bar_exit(bar):
            self.put_event()
            return

        # 必须用「上一根」和「本根」比较，才能抓住交叉瞬间
        golden_cross = self.fast_ma0 > self.slow_ma0 and self.fast_ma1 <= self.slow_ma1
        death_cross = self.fast_ma0 < self.slow_ma0 and self.fast_ma1 >= self.slow_ma1
        decision = decide_v26_entry(
            trend_state=self.trend_filter.state,
            golden_cross=golden_cross,
            death_cross=death_cross,
            trading=bool(self.trading),
            fixed_size=int(self.fixed_size),
            exit_triggered=False,
        )
        if decision.action == ENTRY_ALLOW:
            self.exit_reason = ExitReason.NONE.value
            self.core.set_target_pos(int(decision.target_pos))
            self.write_log(
                f"[V2.6 ENTRY] trend={decision.trend} signal={decision.signal} "
                f"action=ALLOW target_pos={decision.target_pos:+d} "
                f"fast={self.fast_ma0:.4f} slow={self.slow_ma0:.4f} "
                f"strategy_pos={self.core.strategy_pos}"
            )
        elif decision.action == ENTRY_BLOCK:
            self.write_log(
                f"[V2.6 ENTRY FILTER] trend={decision.trend} signal={decision.signal} "
                f"action=BLOCK reason={decision.reason} "
                f"fast={self.fast_ma0:.4f} slow={self.slow_ma0:.4f}"
            )
            self.put_event()
            return
        else:
            self.put_event()
            return

        self.drive_execution(bar)
        self.put_event()

    # ==========================================================
    # 执行
    # ==========================================================

    def drive_execution(self, bar: Optional[BarData] = None):
        """
        向核心要下一动作并执行。三种结果：
        - CANCEL_ALL：目标在挂单期间变了，先撤掉再等回报重算
        - 不该发单：已到目标、有活动单、或闸门未放行
        - 该发单：调用 _send_action 发出一腿（开仓或平昨/平今）
        """
        action = self.core.decide() # 返回值为OrderAction
        self.write_log(
            f"状态机：{action.reason} | kind={action.kind.value}, "
            f"offset={action.offset.value}, "
            f"vol={action.volume}, state={self.core.trading_state.value}"
        )

        if action.kind == ActionKind.CANCEL_ALL:
            self.write_log("撤销未完成订单，等待回报后再根据持仓重算")
            self.cancel_all()
            self._publish_runtime()
            return

        if not action.should_send:
            self._publish_runtime()
            return

        if not self.core.can_send_new_order():
            self.write_log(
                f"当前不允许发新单：system={self.core.system_state.value}, "
                f"reason={self.core.recovery_reason.value}, "
                f"skip={self.core.skip_next_send}"
            )
            self._publish_runtime()
            return

        self._send_action(action, bar)
        self._publish_runtime()

    def _send_action(self, action: OrderAction, bar: Optional[BarData]) -> None:
        """把核心动作映射为 VeighNa 下单；平仓腿带上 CLOSE / CLOSETODAY。"""
        price = self._price_for(action.kind, bar) # 获取价格
        if price <= 0: # 如果价格小于等于0
            self.write_log(f"无有效价格，取消本轮下单：{action.kind.value}")
            self.core.machine.mark_idle()  # 没发出去，状态机不能停在 OPENING/CLOSING
            return # 返回

        offset_kind = action.offset # 获取交易类型
        intent = None
        if action.kind == ActionKind.OPEN_LONG:
            intent = OrderIntent.OPEN_LONG
        elif action.kind == ActionKind.OPEN_SHORT:
            intent = OrderIntent.OPEN_SHORT
        elif action.kind == ActionKind.CLOSE_LONG:
            intent = OrderIntent.CLOSE_LONG
        elif action.kind == ActionKind.CLOSE_SHORT:
            intent = OrderIntent.CLOSE_SHORT
        if intent is None:
            return

        self.core.begin_submit()
        try:
            if action.kind == ActionKind.OPEN_LONG:
                self.write_log(f"发送开多 price={price} volume={action.volume}")
                vt_orderids = self.buy(price, action.volume)
            elif action.kind == ActionKind.OPEN_SHORT:
                self.write_log(f"发送开空 price={price} volume={action.volume}")
                vt_orderids = self.short(price, action.volume)
            elif action.kind == ActionKind.CLOSE_LONG:
                vnpy_offset = self._to_vnpy_offset(offset_kind)
                self.write_log(
                    f"发送平多 offset={offset_kind.value} price={price} volume={action.volume}"
                )
                vt_orderids = self.send_order(Direction.SHORT, vnpy_offset, price, action.volume)
            else:
                vnpy_offset = self._to_vnpy_offset(offset_kind)
                self.write_log(
                    f"发送平空 offset={offset_kind.value} price={price} volume={action.volume}"
                )
                vt_orderids = self.send_order(Direction.LONG, vnpy_offset, price, action.volume)

            if isinstance(vt_orderids, str):
                vt_orderids = [vt_orderids]
            ok = self.core.register_sent_orders(
                vt_orderids,
                intent,
                action.volume,
                offset=offset_kind,
                vt_symbol=self.vt_symbol,
                price=price,
            )
            if not ok:
                self.write_log(self.core.last_message)
        finally:
            self.core.end_submit()

        deferred = self.core.take_deferred_follow()
        if deferred.should_send and self.core.can_send_follow_order():
            self._send_action(deferred, bar)

    def _price_for(self, kind: ActionKind, bar: Optional[BarData]) -> float:
        """
        买入/平空吃卖一；卖出/平多吃买一。
        无 Tick 时退回 K 线收盘价，再叠加 price_offset。
        """
        tick = self.last_tick
        close = bar.close_price if bar is not None else 0.0
        offset = self.price_offset
        if kind in (ActionKind.OPEN_LONG, ActionKind.CLOSE_SHORT):
            # 买入方向：吃卖一，再加偏移提高成交概率
            if tick and tick.ask_price_1 > 0:
                return tick.ask_price_1 + offset
            return close + offset if close else 0.0
        if kind in (ActionKind.OPEN_SHORT, ActionKind.CLOSE_LONG):
            # 卖出方向：吃买一，再减偏移
            if tick and tick.bid_price_1 > 0:
                return tick.bid_price_1 - offset
            return close - offset if close else 0.0
        return close

    # ==========================================================
    # 回报
    # ==========================================================

    def _maybe_send_follow(self, result) -> None:
        if not result.follow.should_send:
            return
        if self.core.can_send_follow_order():
            self._send_action(result.follow, self.last_bar)
        elif self.core.submit_inflight:
            self.core.defer_follow(result.follow)
        else:
            self.write_log("后续平仓腿或反转开仓暂不发送：系统未就绪或仍有活动订单")

    def on_order(self, order: OrderData):
        """
        委托回报：更新核心订单簿；ALLTRADED 且 traded 已满时也会入账策略仓。
        全部撤完且仓位未到目标时，立刻重算下一腿。
        """
        snapshot = self._to_order_snapshot(order)
        self.write_log(
            f"on_order id={snapshot.vt_orderid} intent={snapshot.intent.value} "
            f"status={snapshot.status.value} volume={snapshot.volume} traded={snapshot.traded}"
        )
        result = self.core.on_order(snapshot)
        if result.accepted:
            self.write_log(result.reason)

        if (
            snapshot.status == OrderStatus.CANCELLED
            and self.core.can_send_new_order()
            and self.core.strategy_pos != self.core.target_pos
        ):
            # 撤完后仓位还没到目标（例如金叉时还持着空仓），立刻按现仓重算下一腿
            self.drive_execution(self.last_bar)
        else:
            self._maybe_send_follow(result)

        self._publish_runtime()
        self.put_event()

    def on_trade(self, trade: TradeData):
        """
        成交回报：更新策略仓和今昨仓。
        若 on_order 已经入过账，这里不会再加一次。
        follow.should_send 时继续发下一平仓腿，或反转第二阶段的开仓。
        """
        result = self.core.on_trade(
            str(trade.vt_orderid),
            int(trade.volume),
            tradeid=str(getattr(trade, "vt_tradeid", "") or ""),
        )
        self.write_log(
            f"on_trade tradeid={getattr(trade, 'vt_tradeid', '')} "
            f"orderid={trade.vt_orderid} volume={trade.volume} "
            f"{result.reason}"
        )
        tracker_msg = self.entry_tracker.record(trade)
        self.write_log(tracker_msg)
        pos = int(self.core.strategy_pos)
        if pos == 0:
            self.entry_tracker.clear_position()
            self.exit_engine.reset()
            self.exit_source = ""
        elif self.exit_engine.side == 0 or (
            (self.exit_engine.side > 0) != (pos > 0)
        ):
            self.exit_engine.note_new_position(
                1 if pos > 0 else -1,
                self._current_entry_bar_id(trade),
            )
        self._maybe_send_follow(result)

        self._publish_runtime()
        self.put_event()
        self.sync_data()  # 把变量持久化，重启后能恢复 strategy_pos 等

    def on_stop_order(self, stop_order: StopOrder):
        """本策略不用本地止损单，接口留空。"""
        pass
