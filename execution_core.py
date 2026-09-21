"""
ExecutionCore 2.3.1

净仓开平、成交入账、启动同步、Gateway 对账、今昨拆腿、成交账本。
拷进 VeighNa Station：本文件 + DoubleMaStrategy_V23.py。

不依赖 VeighNa / CTP / 任何期货公司 SDK。
"""

from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Iterable, List, Optional, Set, Tuple


# ============================================================
# 枚举
# ============================================================

# 策略当前正在执行的整体动作,一个策略实例同时只有一个 TradingState
class TradingState(Enum):
    """下单状态机：当前正在做哪一类动作。同一时刻只允许一笔活动委托。    
    策略级别的“执行进度”,状态由 OrderStateMachine 在 decide() 和 on_trade() 中管理
    """
    IDLE = "IDLE"                         # 空闲，可按目标仓重新决策
    OPENING_LONG = "OPENING_LONG"         # 正在开多
    OPENING_SHORT = "OPENING_SHORT"       # 正在开空
    CLOSING_LONG = "CLOSING_LONG"         # 正在平多（含减仓）
    CLOSING_SHORT = "CLOSING_SHORT"       # 正在平空
    REVERSING_TO_LONG = "REVERSING_TO_LONG"    # 先平空，全部平完再开多
    REVERSING_TO_SHORT = "REVERSING_TO_SHORT"  # 先平多，全部平完再开空


# 订单委托意图，策略层发出的单，和策略层对订单的意图 订单级别的“任务目标”
class OrderIntent(Enum):    
    #它描述的是一笔具体订单的意图，是微观层面的定义,在整个订单生命周期内不会改变
    #用于在成交回报（on_trade）中正确更新 strategy_pos（根据意图决定是加仓还是减仓）
    """委托意图。平多用卖出（SHORT+CLOSE），平空用买入（LONG+CLOSE）。"""
    OPEN_LONG = "OPEN_LONG"                # 开多
    OPEN_SHORT = "OPEN_SHORT"              # 开空
    CLOSE_LONG = "CLOSE_LONG"              # 平多
    CLOSE_SHORT = "CLOSE_SHORT"            # 平空
    UNKNOWN = "UNKNOWN"                   # 外部挂单对账时可能无法判断


class OrderStatus(Enum): # 订单级别的“物流状态”
    """委托生命周期。四个视为「活动中」，禁止再发新单。
    每笔订单独立维护自己的 OrderStatus
    """
    SUBMITTING = "SUBMITTING"             # 本地刚发出，尚未收到柜台确认(活动中)
    NOTTRADED = "NOTTRADED"               # 已报入，尚未成交(活动中)
    PARTTRADED = "PARTTRADED"             # 部分成交，剩余量仍挂在市场上（活动中）
    ALLTRADED = "ALLTRADED"               # 终态：全部成交（终态）
    CANCELLED = "CANCELLED"               # 终态：已撤销（可能已有部分成交）（终态）
    REJECTED = "REJECTED"                 # 终态：柜台拒单（终态）
    UNKNOWN = "UNKNOWN"                   # 状态无法识别，进入恢复模式（活动中）


class SystemState(Enum): #系统“红绿灯”
    """决定策略是否允许交易的总闸门。只有 READY / TRADING 才允许自动下单。"""
    STARTING = "STARTING"                  # 刚构造/刚停止，尚未开始同步
    SYNCING_POSITION = "SYNCING_POSITION"  # 正在查账户持仓
    SYNCING_ORDERS = "SYNCING_ORDERS"      # 正在查活动委托
    READY = "READY"                        # 对账通过，空闲可交易
    TRADING = "TRADING"                    # 对账通过，且有在途委托
    RECOVERING = "RECOVERING"              # 仓位不一致或有残留挂单，暂停
    DISCONNECTED = "DISCONNECTED"          # 交易/行情通道断开
    ERROR = "ERROR"                        # 查询失败等硬错误，需要人工处理


class RecoveryReason(Enum): # 故障诊断码
    """进入 RECOVERING / ERROR / DISCONNECTED 的原因，便于面板排查。"""
    NONE = "NONE"
    POSITION_MISMATCH = "POSITION_MISMATCH"  # 策略仓 ≠ 账户仓
    ACTIVE_ORDERS = "ACTIVE_ORDERS"          # 启动时发现未完成委托，不敢自动接管
    QUERY_FAILED = "QUERY_FAILED"            # 持仓或订单查询失败
    DISCONNECTED = "DISCONNECTED"            # 交易/行情通道断开
    ORDER_REJECTED = "ORDER_REJECTED"        # 连续拒单达到阈值
    ORDER_UNKNOWN = "ORDER_UNKNOWN"          # 柜台回报状态无法识别
    MANUAL = "MANUAL"                        # 人工介入


class ActionKind(Enum): #操作指令类型
    """状态机decide()的输出，给策略层的动作类型。"""
    NONE = "NONE"                 # 无需发单
    OPEN_LONG = "OPEN_LONG"      # 开多
    OPEN_SHORT = "OPEN_SHORT"    # 开空 
    CLOSE_LONG = "CLOSE_LONG"    # 平多
    CLOSE_SHORT = "CLOSE_SHORT"  # 平空 
    CANCEL_ALL = "CANCEL_ALL"     # 全部撤销


ACTIVE_ORDER_STATUSES = {  # 活动订单状态集合，用于判断订单是否处于活动中
    OrderStatus.SUBMITTING,
    OrderStatus.NOTTRADED, 
    OrderStatus.PARTTRADED,
    OrderStatus.UNKNOWN, # 状态无法识别，进入恢复模式（活动中），保守处理：认不出状态就当还挂着，禁止再发新单
}


class OffsetKind(Enum):
    """
    CTP 开平标志。
    上期所/能源：平昨必须用 CLOSE，平今必须用 CLOSETODAY，混用会被拒单。
    其他交易所：整笔 CLOSE 即可，柜台自己分配今昨。
    """
    OPEN = "OPEN"
    CLOSE = "CLOSE"                       # 平仓；上期所语义是平昨
    CLOSETODAY = "CLOSETODAY"             # 平今（仅上期所/能源需要显式指定）
    CLOSEYESTERDAY = "CLOSEYESTERDAY"     # 部分接口有此枚举，本核心平昨用 CLOSE


class DirectionKind(Enum):
    """CTP 买卖方向。恢复挂单时必须留下原始方向，不能只留推断出的 intent。"""
    LONG = "LONG"
    SHORT = "SHORT"
    UNKNOWN = "UNKNOWN"


# ============================================================
# 数据结构
# ============================================================

@dataclass
class OrderAction: #决策输出
    """状态机一次决策的结果。volume>0 且 kind 非 NONE/CANCEL_ALL 才真正发单。"""
    kind: ActionKind = ActionKind.NONE   # 动作类型 
    volume: int = 0                      # 动作数量
    next_state: TradingState = TradingState.IDLE # 下一个状态
    pending_target: Optional[int] = None  # 反转时记下待定目标，平完后再开
    reason: str = ""                      # 给人看的决策说明
    offset: OffsetKind = OffsetKind.OPEN  # 开(默认) / 平昨(CLOSE) / 平今(CLOSETODAY)

    @property
    def should_send(self) -> bool: # 是否需要发送单
        # 如果动作类型不是NONE或CANCEL_ALL且数量大于0，则返回True
        return self.kind not in (ActionKind.NONE, ActionKind.CANCEL_ALL) and self.volume > 0 


@dataclass
class PositionSnapshot:
    """
    账户持仓快照。
    net = 多 - 空(多正空负)。V2.3.1 用今/昨字段决定 CLOSE 还是 CLOSETODAY。
    期货账户多空可以同时存在，净仓只是对账用的压缩视图。
    """
    long_volume: int = 0           # 多仓总量 = 今多 + 昨多
    short_volume: int = 0          # 空仓总量
    long_today: int = 0            # 今多
    long_yesterday: int = 0        # 昨多
    short_today: int = 0           # 今空
    short_yesterday: int = 0        # 昨空

    @property
    def net(self) -> int:              # 净仓 = 多仓 - 空仓
        return int(self.long_volume) - int(self.short_volume)

    @classmethod
    def from_net(cls, net: int) -> "PositionSnapshot": #从净仓计算今昨明细
        """只有净仓、没有今昨明细时：全部记为今仓。"""
        net = int(net) # 净仓
        if net >= 0:    # 多仓大于空仓
            return cls(long_volume=net, long_today=net) # 全部记为今仓（多）
        return cls(short_volume=abs(net), short_today=abs(net)) # 全部记为今仓（空）


@dataclass
class OrderSnapshot: # “策略内部使用的标准化订单快照”，包含完整字段和计算属性，用于跟踪订单状态
    """柜台委托的本地副本。direction/offset/price 以柜台值为准，intent 只补一次。"""
    vt_orderid: str          # 委托号
    intent: OrderIntent = OrderIntent.UNKNOWN # 委托意图
    volume: int = 0                # 委托总量
    traded: int = 0                # 柜台回报的已成交量（可能早于 on_trade），成交回报时更新
    applied: int = 0               # 已计入 strategy_pos 的手数；入账只看这个
    status: OrderStatus = OrderStatus.SUBMITTING
    offset: OffsetKind = OffsetKind.OPEN   # 柜台开平：OPEN / CLOSE / CLOSETODAY
    vt_symbol: str = ""
    direction: DirectionKind = DirectionKind.UNKNOWN
    price: float = 0.0

    @property
    def remaining(self) -> int: # 剩余未成交数量
        return max(0, int(self.volume) - int(self.traded))

    @property
    def unapplied(self) -> int: # 未计入 strategy_pos 的手数
        return max(0, int(self.volume) - int(self.applied))

    @property
    def active(self) -> bool: # 是否处于活动中
        return self.status in ACTIVE_ORDER_STATUSES # 活动订单状态集合


def infer_direction_kind(direction) -> DirectionKind:
    name = str(getattr(direction, "name", None) or direction).upper().rsplit(".", 1)[-1]
    if name == "LONG":
        return DirectionKind.LONG
    if name == "SHORT":
        return DirectionKind.SHORT
    return DirectionKind.UNKNOWN


def direction_from_intent(intent: OrderIntent) -> DirectionKind:
    if intent in (OrderIntent.OPEN_LONG, OrderIntent.CLOSE_SHORT):
        return DirectionKind.LONG
    if intent in (OrderIntent.OPEN_SHORT, OrderIntent.CLOSE_LONG):
        return DirectionKind.SHORT
    return DirectionKind.UNKNOWN


def complete_order_snapshot(order: OrderSnapshot) -> OrderSnapshot:
    """只补空缺：已有 direction/offset 不再改写，intent 只在 UNKNOWN 时推断一次。"""
    if order.direction == DirectionKind.UNKNOWN:
        order.direction = direction_from_intent(order.intent)
    if order.intent == OrderIntent.UNKNOWN:
        order.intent = infer_order_intent(order.direction, order.offset)
    return order


def infer_order_intent(direction, offset) -> OrderIntent:
    """卖出平仓=平多，买入平仓=平空；开仓按方向。"""
    d = str(getattr(direction, "name", None) or direction).upper().rsplit(".", 1)[-1]
    o = str(getattr(offset, "name", None) or offset).upper().rsplit(".", 1)[-1]
    closing = o in ("CLOSE", "CLOSETODAY", "CLOSEYESTERDAY")
    if closing and d == "SHORT":
        return OrderIntent.CLOSE_LONG
    if closing and d == "LONG":
        return OrderIntent.CLOSE_SHORT
    if d == "LONG":
        return OrderIntent.OPEN_LONG
    if d == "SHORT":
        return OrderIntent.OPEN_SHORT
    return OrderIntent.UNKNOWN


def infer_offset_kind(offset) -> OffsetKind:
    name = str(getattr(offset, "name", None) or offset).upper().rsplit(".", 1)[-1]
    if name == "CLOSETODAY":
        return OffsetKind.CLOSETODAY
    if name == "CLOSEYESTERDAY":
        return OffsetKind.CLOSEYESTERDAY
    if name == "CLOSE":
        return OffsetKind.CLOSE
    return OffsetKind.OPEN


@dataclass
class SyncReport:
    """一次启动/重连对账的结果，交给 RecoveryManager 决定能否放行。"""
    query_ok: bool  # 查询是否成功
    strategy_pos: int  # 策略仓位
    account_pos: int  # 账户仓位
    active_orders: List[OrderSnapshot] = field(default_factory=list) # 活动订单
    reason: str = ""  # 原因
    position_query_ok: bool = True   # 持仓查询是否成功
    order_query_ok: bool = True      # 活动单查询是否成功

    @property
    def matched(self) -> bool: # 是否匹配
        return self.query_ok and self.strategy_pos == self.account_pos # 查询成功且策略仓位等于账户仓位

    @property
    def has_active_orders(self) -> bool: # 是否有活动订单
        return any(order.active for order in self.active_orders) # 活动订单状态集合


@dataclass
class TradeApplyResult: #结果是什么，下一步怎么办
    """成交处理结果。follow 可能是下一平仓腿，或反转后的开仓。"""
    accepted: bool                 # True=本次回调新计入了 strategy_pos
    ignored: bool                  # True=重复/未知/已由另一路回报入账，未再改仓
    reason: str
    follow: OrderAction = field(default_factory=OrderAction)
    applied_volume: int = 0        # 本次新计入手数（可能小于回报手数）


# ============================================================
# 订单状态机（V2.1 / V2.2 保留）
# ============================================================

class OrderStateMachine:
    """
    净仓开平决策。硬规则：
    1. 有活动订单时不新开/平仓单。
    2. 反向时只先平仓，全部平完且无活动单才开反向。
    3. pos == 0 且无活动订单才允许开反向仓。
    4. 未完成订单期间目标变化：只撤单，撤完再按实际持仓重算。
    5. 撤单/拒单后回到 IDLE。
    """

    def __init__(self) -> None:
        self.state: TradingState = TradingState.IDLE # 当前状态
        self.active_orderids: Set[str] = set() # 活动订单ID集合
        self.order_intents: Dict[str, OrderIntent] = {} # 活动订单意图字典
        self.pending_target_pos: Optional[int] = None  # 反转最终目标
        self.working_target: Optional[int] = None      # 当前这批单发出时的目标
        self.cancel_requested: bool = False            # 防止重复发出 CANCEL_ALL，避免重复撤单

    def reset(self) -> None:
        self.state = TradingState.IDLE
        self.active_orderids.clear()
        self.order_intents.clear()
        self.pending_target_pos = None
        self.working_target = None
        self.cancel_requested = False

    def has_active_orders(self) -> bool:
        return bool(self.active_orderids)

    def register_order(self, vt_orderid: str, intent: OrderIntent, target_pos: int) -> None:
        """本地发出或从账户同步到一笔活动委托。"""
        self.active_orderids.add(vt_orderid)
        self.order_intents[vt_orderid] = intent
        self.working_target = target_pos

    def finish_order(self, vt_orderid: str) -> None:
        """委托终态（全成/撤/拒）后从活动集合移除。"""
        self.active_orderids.discard(vt_orderid) # 从活动集合移除
        if not self.active_orderids: # 如果活动集合为空
            self.working_target = None # 当前这批单发出时的目标
            self.cancel_requested = False # 防止重复发出 CANCEL_ALL，避免重复撤单

    def mark_idle(self) -> None:   # 将状态机强制置回空闲状态
        self.state = TradingState.IDLE # 状态为空闲
        self.pending_target_pos = None # 待定目标
        if not self.active_orderids: # 如果活动集合为空
            self.working_target = None # 当前这批单发出时的目标
            self.cancel_requested = False # 防止重复发出 CANCEL_ALL，避免重复撤单

    def on_cancelled(self) -> None: 
        self.mark_idle() # 标记为空闲状态

    def on_rejected(self) -> None: # 委托被拒后将状态机强制置回空闲状态
        self.mark_idle()

    def decide(self, current_pos: int, target_pos: int) -> OrderAction:
        """
        根据当前净仓和目标净仓给出下一动作。
        多仓为正、空仓为负；加减仓走开/平，翻仓先平后开。
        """
        if self.active_orderids:
            # 有挂单时绝对不发新开/平。目标变了只撤一次，等撤完回报再 decide。
            if (
                self.working_target is not None
                and self.working_target != target_pos
                and not self.cancel_requested
            ):
                self.cancel_requested = True
                return OrderAction(
                    kind=ActionKind.CANCEL_ALL,
                    reason="目标仓位在订单未完成期间发生变化，先撤单",
                )
            return OrderAction(reason="当前存在活动订单，暂不重复下单")

        if current_pos == target_pos: # 当前仓位等于目标仓位current_pos 同strategy_pos
            self.mark_idle()
            return OrderAction(reason="已达到目标仓位")

        # 1.当前持多：
        if current_pos > 0: 
            # 1.1.目标<=0 先把多仓平光（目标<0 则标记反转，平完再开空）；否则加多或减多
            if target_pos <= 0:
                reversing = target_pos < 0   # 目标仓位为负，即目标策略仓中持有空仓
                self.state = (
                    # 如果目标仓位为负，则将状态机强制置回反转开空状态，否则置回平多状态
                    TradingState.REVERSING_TO_SHORT if reversing else TradingState.CLOSING_LONG 
                )
                self.pending_target_pos = target_pos if reversing else None

                return OrderAction(
                    kind=ActionKind.CLOSE_LONG,
                    volume=current_pos,  # 翻仓/清仓必须一次平光，不能留残仓
                    next_state=self.state,
                    pending_target=self.pending_target_pos,
                    reason="平多" + ("（准备反转开空）" if reversing else ""),
                )
            # 1.2.当前仓位大于目标仓位，即当前策略仓中持有的多仓大于目标策略仓中持有的多仓
            if current_pos > target_pos: # 则将状态机强制置回平多状态
                self.state = TradingState.CLOSING_LONG
                self.pending_target_pos = None
                return OrderAction(
                    kind=ActionKind.CLOSE_LONG,
                    volume=current_pos - target_pos,  # 只平多出来的那部分
                    next_state=self.state,
                    reason="减多",
                )
            # 1.3.当前仓位小于目标仓位，即当前策略仓中持有的多仓小于目标策略仓中持有的多仓
            self.state = TradingState.OPENING_LONG
            self.pending_target_pos = None
            return OrderAction(
                kind=ActionKind.OPEN_LONG,
                volume=target_pos - current_pos,
                next_state=self.state,
                reason="加多",
            )

        # 2.当前持空：
        if current_pos < 0:
            # 2.1.目标>=0 先平光（可能随后反转开多）；否则加空或减空
            if target_pos >= 0:
                reversing = target_pos > 0  # 目标仓位为正，即目标策略仓中持有多仓
                self.state = (
                    TradingState.REVERSING_TO_LONG if reversing else TradingState.CLOSING_SHORT
                )
                self.pending_target_pos = target_pos if reversing else None
                return OrderAction(
                    kind=ActionKind.CLOSE_SHORT,
                    volume=abs(current_pos),
                    next_state=self.state,
                    pending_target=self.pending_target_pos,
                    reason="平空" + ("（准备反转开多）" if reversing else ""),
                )
            # 2.2.当前仓位大于目标仓位，即当前策略仓中持有的空仓大于目标策略仓中持有的空仓
            if abs(current_pos) > abs(target_pos):
                self.state = TradingState.CLOSING_SHORT
                self.pending_target_pos = None
                return OrderAction(
                    kind=ActionKind.CLOSE_SHORT,
                    volume=abs(current_pos) - abs(target_pos),
                    next_state=self.state,
                    reason="减空",
                )
            # 2.3.当前仓位小于目标仓位，即当前策略仓中持有的空仓小于目标策略仓中持有的空仓
            self.state = TradingState.OPENING_SHORT
            self.pending_target_pos = None
            return OrderAction(
                kind=ActionKind.OPEN_SHORT,
                volume=abs(target_pos) - abs(current_pos),
                next_state=self.state,
                reason="加空",
            )

        # 3当前空仓(current_pos==0)：按目标直接开多或开空
        if target_pos > 0: # 3.1目标仓位为正，即目标策略仓中持有多仓
            self.state = TradingState.OPENING_LONG
            self.pending_target_pos = None
            return OrderAction(
                kind=ActionKind.OPEN_LONG,
                volume=target_pos,
                next_state=self.state,
                reason="开多",
            )
        if target_pos < 0:
            self.state = TradingState.OPENING_SHORT
            self.pending_target_pos = None
            return OrderAction(
                kind=ActionKind.OPEN_SHORT,
                volume=abs(target_pos),
                next_state=self.state,
                reason="开空",
            )
        # 4.理论上是“不应该发生”的兜底逻辑
        self.mark_idle()
        return OrderAction(reason="无动作")


    def on_trade(self, current_pos: int, target_pos: int) -> OrderAction:
        """成交后看是否需要立刻跟下一动作（主要是反转第二阶段开仓）。"""
        reversing = self.state in (
            TradingState.REVERSING_TO_LONG,
            TradingState.REVERSING_TO_SHORT,
        )
        if reversing:
            if current_pos != 0:
                return OrderAction(reason="反转平仓尚未完成，不能开反向仓")
            if self.active_orderids:
                return OrderAction(reason="反转等待：仍存在活动订单")
            # 原方向已平光且无挂单，立刻给出反向开仓动作
            return self._open_after_flat(target_pos) 

        if self.state in (TradingState.OPENING_LONG, TradingState.OPENING_SHORT):
            if current_pos == target_pos and not self.active_orderids:
                self.mark_idle()
                return OrderAction(reason="开仓完成")
            return OrderAction(reason="开仓部分成交，等待剩余")

        if self.state in (TradingState.CLOSING_LONG, TradingState.CLOSING_SHORT):
            if current_pos == target_pos and not self.active_orderids:
                self.mark_idle()
                return OrderAction(reason="平仓完成")
            return OrderAction(reason="平仓部分成交，等待剩余")

        if current_pos == target_pos and not self.active_orderids:
            self.mark_idle()
        return OrderAction(reason="成交已记录")


    def _open_after_flat(self, target_pos: int) -> OrderAction:
        """反转：原方向已平完，按最终目标开反向仓。"""
        self.pending_target_pos = None
        if target_pos > 0:
            self.state = TradingState.OPENING_LONG
            return OrderAction(
                kind=ActionKind.OPEN_LONG,
                volume=target_pos,
                next_state=self.state,
                reason="反转第二阶段：开多",
            )
        if target_pos < 0:
            self.state = TradingState.OPENING_SHORT
            return OrderAction(
                kind=ActionKind.OPEN_SHORT,
                volume=abs(target_pos),
                next_state=self.state,
                reason="反转第二阶段：开空",
            )
        self.mark_idle()
        return OrderAction(reason="反转后目标为0，无需开仓")


# ============================================================
# 持仓对账 / 恢复闸门
# ============================================================

class PositionSyncManager:
    """保存最近一次账户持仓快照，以及据此算出的净仓 account_pos。"""

    def __init__(self) -> None:
        self.snapshot: Optional[PositionSnapshot] = None  # 最近一次账户持仓快照
        self.synced: bool = False  # 是否已同步
        self.account_pos: int = 0  # 账户净仓

    def reset(self) -> None: # 重置所有状态，清除持仓快照
        self.snapshot = None
        self.synced = False
        self.account_pos = 0

    def apply_snapshot(self, snapshot: PositionSnapshot) -> None:
        self.snapshot = snapshot
        self.account_pos = snapshot.net
        self.synced = True

    def build_report( 
        self,
        strategy_pos: int,
        account_pos: int,
        active_orders: Optional[List[OrderSnapshot]] = None,
        query_ok: bool = True,
        reason: str = "",
        position_query_ok: bool = True,
        order_query_ok: bool = True,
    ) -> SyncReport:
        """组装对账报告。未传 reason 时按「查询失败 / 仓位不一致 / 有挂单」自动填写。"""
        snapshots = list(active_orders or [])
        if not query_ok and not reason:
            reason = "持仓或活动订单查询失败"
        elif query_ok and int(strategy_pos) != int(account_pos) and not reason:
            reason = (
                f"持仓不一致：strategy_pos={strategy_pos}, account_pos={account_pos}"
            )
        elif query_ok and snapshots and not reason:
            reason = f"启动时存在 {len(snapshots)} 笔活动订单"
        return SyncReport(
            query_ok=query_ok,
            strategy_pos=int(strategy_pos),
            account_pos=int(account_pos),
            active_orders=snapshots,
            reason=reason,
            position_query_ok=position_query_ok,
            order_query_ok=order_query_ok,
        )


class RecoveryManager:
    """
    交易许可闸门。
    查询失败 → ERROR；仓位不一致/有挂单 → RECOVERING；
    对账通过才 trading_enabled=True。
    """

    def __init__(self) -> None:
        self.system_state: SystemState = SystemState.STARTING
        self.trading_enabled: bool = False  # 总开关，ERROR/RECOVERING/断开时为 False
        self.recovery_reason: RecoveryReason = RecoveryReason.NONE
        self.last_report: Optional[SyncReport] = None
        self.last_message: str = ""

    def on_boot(self) -> None:
        """启动后先禁单，进入持仓同步。"""
        self.system_state = SystemState.SYNCING_POSITION
        self.trading_enabled = False
        self.recovery_reason = RecoveryReason.NONE
        self.last_message = "启动后进入持仓同步"

    def on_disconnect(self) -> None:
        """通道断开：立刻禁单。重连前不允许发任何单。"""
        self.system_state = SystemState.DISCONNECTED
        self.trading_enabled = False
        self.recovery_reason = RecoveryReason.DISCONNECTED
        self.last_message = "CTP/行情断开，暂停下单"

    def on_reconnect(self) -> None:
        """重连后不能直接 READY，必须重新走两阶段同步。"""
        self.system_state = SystemState.SYNCING_POSITION
        self.trading_enabled = False
        self.recovery_reason = RecoveryReason.NONE
        self.last_message = "重连后重新同步持仓和活动订单"

    def enter_error(self, message: str, reason: RecoveryReason = RecoveryReason.QUERY_FAILED) -> None:
        self.system_state = SystemState.ERROR
        self.trading_enabled = False
        self.recovery_reason = reason
        self.last_message = message

    def enter_recovering(self, reason: RecoveryReason, message: str) -> None:
        self.system_state = SystemState.RECOVERING
        self.trading_enabled = False
        self.recovery_reason = reason
        self.last_message = message

    def force_ready(self, message: str = "已就绪") -> None:
        self.system_state = SystemState.READY
        self.trading_enabled = True
        self.recovery_reason = RecoveryReason.NONE
        self.last_message = message

    def mark_trading(self) -> None:
        """有在途委托时把 READY 标成 TRADING，便于面板区分。"""
        if self.can_trade():
            self.system_state = SystemState.TRADING

    def mark_ready_idle(self) -> None:
        """全部成交且仓位到位后，从 TRADING 回到 READY。"""
        if self.system_state == SystemState.TRADING and self.trading_enabled:
            self.system_state = SystemState.READY

    def apply_sync(self,report: SyncReport,adopt_mismatch: bool = False) -> SystemState:
        """两阶段同步结束后的放行判断。adopt_mismatch=True 时，仓位不一致也按账户仓接管并放行。"""
        # 记录最近一次同步报告
        self.last_report = report

        # 1.查询失败是硬错误：不知道账户真实状态，禁止猜测
        if not report.position_query_ok or (not report.query_ok and not report.order_query_ok):
            self.enter_error(report.reason or "查询失败")
            return self.system_state
        if not report.order_query_ok:
            self.enter_error(report.reason or "活动订单查询失败")
            return self.system_state
        if not report.query_ok:
            self.enter_error(report.reason or "查询失败")
            return self.system_state

        # 2.启动时还有挂单：可能是上次崩溃残留，自动交易会把仓位搞乱
        if report.has_active_orders:
            self.enter_recovering(
                RecoveryReason.ACTIVE_ORDERS,
                report.reason or "存在活动订单，禁止自动交易",
            )
            return self.system_state

        # 3.仓位不一致：需要人工确认是否采用账户仓
        if not report.matched:
            if adopt_mismatch:
                # 3.1.明确允许「以账户为准」时才接管，否则停在 RECOVERING 等人确认
                self.force_ready(
                    f"持仓不一致，已按账户持仓接管：account_pos={report.account_pos}"
                )
                return self.system_state
            self.enter_recovering(
                RecoveryReason.POSITION_MISMATCH,
                report.reason or "持仓不一致，等待确认",
            )
            return self.system_state

        # 4.仓位对账一致，允许交易
        self.force_ready("持仓对账一致，允许交易")
        return self.system_state

    def confirm_adopt_account(self, account_pos: int) -> int:
        """人工确认采用账户仓后放行，返回应写入 strategy_pos 的值。"""
        self.force_ready(f"人工确认采用账户持仓 account_pos={account_pos}")
        return int(account_pos)

    def can_trade(self) -> bool:
        return self.trading_enabled and self.system_state in (
            SystemState.READY,
            SystemState.TRADING,
        )


def _as_vt_orderids(vt_orderids) -> List[str]:
    """buy()/send_order() 有的版本返回 list，有的返回单个字符串。
    将 VeighNa 下单函数的返回值统一转换为字符串列表(list[str])"""
    if vt_orderids is None:
        return []
    if isinstance(vt_orderids, (str, bytes)):
        text = str(vt_orderids).strip()
        return [text] if text else []
    result: List[str] = []
    for item in vt_orderids:
        if item is None:
            continue
        text = str(item).strip()
        if text:
            result.append(text)
    return result


def _apply_intent(pos: int, intent: OrderIntent, volume: int) -> int:
    """
    按意图把成交手数加到净仓上（多正空负）。
    开多/平空 → 净仓增加；开空/平多 → 净仓减少。
    """
    if intent == OrderIntent.OPEN_LONG:
        return pos + volume
    if intent == OrderIntent.OPEN_SHORT:
        return pos - volume
    if intent == OrderIntent.CLOSE_LONG:
        return pos - volume
    if intent == OrderIntent.CLOSE_SHORT:
        return pos + volume
    return pos


# ============================================================
# Gateway / 周期对账
# ============================================================

@dataclass
class GatewayFlags:
    """交易通道 / 行情通道是否连通。两者都 True 才算 connected。"""
    td_ok: bool = True
    md_ok: bool = True

    @property
    def connected(self) -> bool:
        return bool(self.td_ok) and bool(self.md_ok)


@dataclass
class ReconcileResult:
    """
    周期对账结果。
    action: none / skipped(跳过) / matched(对账一致) / mismatch(仓位不一致) / recovered(恢复状态) / lost(连接断开)
    """
    action: str = "none"
    reason: str = ""
    account_pos: int = 0
    strategy_pos: int = 0

# ============================================================
# 今仓/昨仓 + CLOSE/CLOSETODAY
# ============================================================

SPLIT_EXCHANGES = {"SHFE", "INE"}          # 上期所 / 上期能源：必须拆平今/平昨
NO_SPLIT_EXCHANGES = {"DCE", "CZCE", "CFFEX", "GFEX"}  # 整笔 CLOSE
KNOWN_EXCHANGES = tuple(SPLIT_EXCHANGES | NO_SPLIT_EXCHANGES)

INTENT_TO_KIND = {
    OrderIntent.OPEN_LONG: ActionKind.OPEN_LONG,
    OrderIntent.OPEN_SHORT: ActionKind.OPEN_SHORT,
    OrderIntent.CLOSE_LONG: ActionKind.CLOSE_LONG,
    OrderIntent.CLOSE_SHORT: ActionKind.CLOSE_SHORT,
}


def copy_snapshot(snapshot: PositionSnapshot) -> PositionSnapshot:
    """深拷贝持仓快照，避免工作仓被外部引用改掉。"""
    return PositionSnapshot(
        long_volume=int(snapshot.long_volume),
        short_volume=int(snapshot.short_volume),
        long_today=int(snapshot.long_today),
        long_yesterday=int(snapshot.long_yesterday),
        short_today=int(snapshot.short_today),
        short_yesterday=int(snapshot.short_yesterday),
    )

# 工具函数 :将各种形式的交易所标识统一标准化为交易所代码,输出如：SHFE、DCE、或者空字符串
def normalize_exchange(text) -> str:
    """把 Exchange 枚举 / 'rb2401.SHFE' / 字符串统一成 SHFE 这类代码。"""
    if text is None:
        return ""
    name = getattr(text, "name", None)
    if name:
        upper_name = str(name).upper()
        if upper_name in KNOWN_EXCHANGES:
            return upper_name
    value = getattr(text, "value", None)
    raw = str(value if value is not None else text).upper()
    for exchange in KNOWN_EXCHANGES:
        if exchange in raw:
            return exchange
    if "." in raw:
        return raw.rsplit(".", 1)[-1].strip()
    return raw.strip()


def requires_split(exchange: str) -> Optional[bool]:
    """
    True  = 必须拆平今/平昨
    False = 整笔 CLOSE
    None  = 交易所未知，按持仓结构保守处理
    """
    name = normalize_exchange(exchange)
    if name in SPLIT_EXCHANGES:
        return True
    if name in NO_SPLIT_EXCHANGES:
        return False
    return None


# 规范化今昨持仓：将 PositionSnapshot 中的今昨持仓补齐为总量
def normalize_td_yd(snapshot: PositionSnapshot) -> PositionSnapshot:
    """
    补齐今/昨：
    - 有总量但今昨都为 0 → 全部视为今仓（与 PositionSnapshot.from_net 一致）
    - 今+昨 ≠ 总量 → 信任昨仓，今仓 = 总量 - 昨仓
    """
    snap = copy_snapshot(snapshot)

    def _fix(volume: int, today: int, yesterday: int) -> tuple[int, int]:
        volume = max(0, int(volume))
        today = max(0, int(today))
        yesterday = max(0, int(yesterday))

        if volume <= 0:
            return 0, 0
        if today + yesterday == 0:
            return volume, 0  # 只有总量、没有今昨明细 → 全部当今回
        if today + yesterday != volume:
            yesterday = min(yesterday, volume)  # 信任昨仓，用总量倒挤今仓
            today = max(0, volume - yesterday)
        return today, yesterday

    snap.long_today, snap.long_yesterday = _fix(
        snap.long_volume, snap.long_today, snap.long_yesterday
    )
    snap.short_today, snap.short_yesterday = _fix(
        snap.short_volume, snap.short_today, snap.short_yesterday
    )
    snap.long_volume = snap.long_today + snap.long_yesterday
    snap.short_volume = snap.short_today + snap.short_yesterday
    return snap

def _close_leg(
    kind: ActionKind,
    volume: int,
    offset: OffsetKind,
    reason: str,
    next_state: TradingState = TradingState.IDLE,
    pending_target: Optional[int] = None,
) -> OrderAction:
    """构造一腿平仓动作（平昨或平今）。"""
    return OrderAction(
        kind=kind,
        volume=int(volume),
        next_state=next_state,
        pending_target=pending_target,
        reason=reason,
        offset=offset,
    )


def plan_close_long(
    volume: int,
    snapshot: PositionSnapshot,
    exchange: str = "",
    next_state: TradingState = TradingState.IDLE,
    pending_target: Optional[int] = None,
) -> List[OrderAction]:
    """按今昨把「平多 volume 手」拆成顺序腿：先昨后今。"""
    snap = normalize_td_yd(snapshot)
    volume = min(max(0, int(volume)), snap.long_volume)
    if volume <= 0:
        return []
    return _plan_close_side(
        kind=ActionKind.CLOSE_LONG,
        volume=volume,
        today=snap.long_today,
        yesterday=snap.long_yesterday,
        side_name="多",
        exchange=exchange,
        next_state=next_state,
        pending_target=pending_target,
    )


def plan_close_short(
    volume: int,
    snapshot: PositionSnapshot,
    exchange: str = "",
    next_state: TradingState = TradingState.IDLE,
    pending_target: Optional[int] = None,
) -> List[OrderAction]:
    """按今昨把「平空 volume 手」拆成顺序腿：先昨后今。"""
    snap = normalize_td_yd(snapshot)
    volume = min(max(0, int(volume)), snap.short_volume)
    if volume <= 0:
        return []
    return _plan_close_side(
        kind=ActionKind.CLOSE_SHORT,
        volume=volume,
        today=snap.short_today,
        yesterday=snap.short_yesterday,
        side_name="空",
        exchange=exchange,
        next_state=next_state,
        pending_target=pending_target,
    )


def _plan_close_side(
    kind: ActionKind,
    volume: int,
    today: int,
    yesterday: int,
    side_name: str,
    exchange: str,
    next_state: TradingState,
    pending_target: Optional[int],
) -> List[OrderAction]:
    """
    按交易所规则拆平仓腿：
    - 大商/郑商/中金/广期：整笔 CLOSE
    - 上期/能源：先 CLOSE 平昨，再 CLOSETODAY 平今
    - 未知交易所：有昨先平昨，否则当今回 CLOSETODAY
    """
    split = requires_split(exchange)
    exchange_name = normalize_exchange(exchange) or "未知交易所"

    if split is False:
        # 大商/郑商/中金/广期：柜台接受整笔 CLOSE，不需要拆
        return [
            _close_leg(
                kind,
                volume,
                OffsetKind.CLOSE,
                f"平{side_name} {volume} 手（{exchange_name} CLOSE）",
                next_state,
                pending_target,
            )
        ]

    if split is None:
        # 交易所未知：有昨仓就先平昨（更安全），纯今仓才用 CLOSETODAY
        if yesterday <= 0:
            return [
                _close_leg(
                    kind,
                    volume,
                    OffsetKind.CLOSETODAY,
                    f"平今{side_name} {volume} 手（交易所未知，按今仓 CLOSETODAY）",
                    next_state,
                    pending_target,
                )
            ]
        if today <= 0:
            return [
                _close_leg(
                    kind,
                    volume,
                    OffsetKind.CLOSE,
                    f"平昨{side_name} {volume} 手（交易所未知）",
                    next_state,
                    pending_target,
                )
            ]

    # 先昨后今：剩余手数依次从昨仓、今仓取
    yd = min(volume, max(0, yesterday))
    td = min(volume - yd, max(0, today))
    legs: List[OrderAction] = []
    if yd:
        legs.append(
            _close_leg(
                kind,
                yd,
                OffsetKind.CLOSE,
                f"平昨{side_name} {yd} 手（{exchange_name} CLOSE）",
                next_state,
                pending_target,
            )
        )
    if td:
        legs.append(
            _close_leg(
                kind,
                td,
                OffsetKind.CLOSETODAY,
                f"平今{side_name} {td} 手（{exchange_name} CLOSETODAY）",
                next_state,
                pending_target,
            )
        )
    if not legs and volume:
        # 今昨字段都对不上请求手数时的兜底，避免完全发不出平仓单
        legs.append(
            _close_leg(
                kind,
                volume,
                OffsetKind.CLOSETODAY,
                f"平今{side_name} {volume} 手（今昨不足时回退 CLOSETODAY）",
                next_state,
                pending_target,
            )
        )
    return legs

# 消耗
def _consume_side(today: int, yesterday: int, volume: int, offset: OffsetKind) -> tuple[int, int]:
    """
    成交后从今/昨扣减。优先扣 offset 指定的那一侧，不够再从另一侧补。
    用于本地 working_position，避免等账户推送。
    """
    volume = max(0, int(volume))
    today = max(0, int(today))
    yesterday = max(0, int(yesterday))
    if offset == OffsetKind.CLOSETODAY:
        take = min(volume, today)
        today -= take
        rest = volume - take
        yd_take = min(rest, yesterday)  # 今仓不够时从昨仓补扣（异常保护）
        yesterday -= yd_take
    else:
        # CLOSE / CLOSEYESTERDAY：先扣昨仓，不够再扣今仓
        take = min(volume, yesterday)
        yesterday -= take
        rest = volume - take
        td_take = min(rest, today)
        today -= td_take
    return today, yesterday

# ============================================================
# 成交账本
# ============================================================

TRADE_LEDGER_KEEP_SECONDS = 3 * 24 * 3600  # CTP 成交编号隔日可能复用，只留近 3 天


def default_trade_ledger_path(strategy_name: str, vt_symbol: str, base_dir: str) -> str:
    raw = f"{strategy_name}_{vt_symbol}"
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in raw)
    return os.path.join(base_dir, "ledger", f"{safe}.sqlite")


class TradeLedger:
    """
    成交幂等账本。不挂文件时只记内存；挂上 SQLite 后重启仍能认出已处理成交。
    """

    def __init__(self) -> None:
        self.ids: Set[str] = set()
        self.path: str = ""
        self._conn: Optional[sqlite3.Connection] = None

    def seen(self, tradeid: str) -> bool:
        return bool(tradeid) and tradeid in self.ids

    def remember(self, tradeid: str, orderid: str = "", volume: int = 0) -> None:
        if not tradeid or tradeid in self.ids:
            return
        self.ids.add(tradeid)
        if self._conn is None:
            return
        try:
            self._conn.execute(
                "INSERT OR IGNORE INTO trades(tradeid, orderid, volume, ts) VALUES (?,?,?,?)",
                (tradeid, str(orderid), int(volume), time.time()),
            )
            self._conn.commit()
        except sqlite3.Error:
            pass

    def attach(self, path: str) -> None:
        path = os.path.abspath(path)
        if self._conn is not None and self.path == path:
            self._reload()
            return
        self.close()
        folder = os.path.dirname(path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS trades (
                tradeid TEXT PRIMARY KEY,
                orderid TEXT NOT NULL DEFAULT '',
                volume INTEGER NOT NULL DEFAULT 0,
                ts REAL NOT NULL
            )
            """
        )
        cutoff = time.time() - TRADE_LEDGER_KEEP_SECONDS
        conn.execute("DELETE FROM trades WHERE ts < ?", (cutoff,))
        conn.commit()
        self._conn = conn
        self.path = path
        self._reload()

    def reset_memory(self) -> None:
        self.ids.clear()

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                self._conn.close()
            except sqlite3.Error:
                pass
        self._conn = None
        self.path = ""

    def _reload(self) -> None:
        if self._conn is None:
            self.ids = set()
            return
        self.ids = {row[0] for row in self._conn.execute("SELECT tradeid FROM trades")}


# ============================================================
# ExecutionCore
# ============================================================

class ExecutionCore:
    """
    执行核心：净仓开平、成交入账、启动同步、Gateway 对账、今昨拆腿。

    启动时序：
        start()
          ↓
        SYNCING_POSITION
          ↓
        sync_position()
          ↓
        SYNCING_ORDERS
          ↓
        sync_orders()
          ↓
        READY / RECOVERING / ERROR
    """

    VERSION = "2.3.1"

    def __init__(self) -> None:
        self.version = self.VERSION # 版本号
        self.machine = OrderStateMachine() # 状态机
        self.position_sync = PositionSyncManager() # 持仓同步管理器
        self.recovery = RecoveryManager() # 恢复管理器

        self.strategy_pos: int = 0 # 策略仓
        self.target_pos: int = 0 # 目标仓
        self.safety_fuse: bool = True # 安全熔断
        self.adopt_account_on_mismatch: bool = False # 是否允许账户仓覆盖策略仓

        self.active_orders: Dict[str, OrderSnapshot] = {} # 活动订单
        self.order_history: Dict[str, OrderSnapshot] = {} # 订单历史
        self.trade_ledger = TradeLedger()
        self.pending_trades: Dict[str, List[Tuple[int, str]]] = {} # 订单未登记时暂存：vt_orderid -> [(volume, tradeid)]
        self.pending_trade_limit: int = 20

        self.skip_next_send: bool = False # 是否跳过下一轮发送
        self.reject_count: int = 0 # 拒绝次数
        self.last_message: str = "" # 最后一条消息
        self._pending_follow: OrderAction = OrderAction() # 待发送的订单动作
        self.submit_inflight: bool = False # buy() 尚未登记，禁止重入再发
        self._deferred_follow: OrderAction = OrderAction() # 发送中被拦住的跟单

        self.gateway = GatewayFlags() # 网关标志
        self.was_disconnected: bool = False # 是否断开连接
        self.bars_since_reconcile: int = 0 # 上次对账以来经过的K线数
        self.reconcile_every_bars: int = 1 # 每隔多少根K线对账一次
        self.last_position_snapshot: Optional[PositionSnapshot] = None # 最近一次账户持仓快照
        self.mismatch_grace: int = 0 # 净仓未对齐已连续几次（缓存滞后宽限）
        self.mismatch_grace_limit: int = 2 # 超过这次数仍不一致才 RECOVERING

        self.exchange: str = "" # 交易所
        self.vt_symbol: str = ""
        self.working_position: PositionSnapshot = PositionSnapshot() # 当前工作仓位
        self.close_leg_queue: List[OrderAction] = [] # 平仓队列
        self.order_offsets: Dict[str, OffsetKind] = {} # 订单偏移

    # ---------- 只读视图 ----------

    @property
    def system_state(self) -> SystemState:
        return self.recovery.system_state # 系统状态

    @property
    def recovery_reason(self) -> RecoveryReason:
        return self.recovery.recovery_reason # 恢复原因

    @property
    def trading_state(self) -> TradingState:
        return self.machine.state # 交易状态

    @property
    def account_pos(self) -> int:
        return int(self.position_sync.account_pos) # 账户净仓

    @property
    def synced(self) -> bool:
        return self.position_sync.synced # 是否已同步持仓

    @property
    def processed_trade_ids(self) -> Set[str]:
        return self.trade_ledger.ids

    def attach_trade_ledger(self, path: str) -> bool:
        """挂上 SQLite 成交账本并载入未过期记录。打开失败则本会话只记内存。"""
        try:
            self.trade_ledger.attach(path)
            self.last_message = (
                f"成交账本已加载: {self.trade_ledger.path} ({len(self.trade_ledger.ids)} 条)"
            )
            return True
        except (OSError, sqlite3.Error) as exc:
            self.last_message = f"成交账本打开失败，本会话仅内存去重: {exc}"
            return False

    # ---------- 生命周期 ----------

    def start(self) -> None:
        """进入同步流程，清空活动单；不改 strategy_pos，留给随后的账户查询。"""
        self.recovery.on_boot()
        self.position_sync.reset()
        self.active_orders.clear()
        self.pending_trades.clear()
        self.skip_next_send = False
        self.reject_count = 0
        self._pending_follow = OrderAction()
        self.submit_inflight = False
        self._deferred_follow = OrderAction()
        self.mismatch_grace = 0
        self.last_message = self.recovery.last_message

    def ready_from_backtest(self, pos: int = 0) -> None:
        """回测：账户仓 = 策略仓，跳过 CTP 查询。"""
        pos = int(pos)
        self.strategy_pos = pos
        self.target_pos = pos
        self.position_sync.apply_snapshot(PositionSnapshot.from_net(pos))
        self.machine.reset()
        self.active_orders.clear()
        self.skip_next_send = False
        self.reject_count = 0
        self._pending_follow = OrderAction()
        self.recovery.force_ready("回测模式：跳过 CTP 持仓查询")
        self.last_message = self.recovery.last_message
        self.gateway = GatewayFlags(True, True)
        self.was_disconnected = False
        self.bars_since_reconcile = 0
        self.last_position_snapshot = PositionSnapshot.from_net(pos)
        self.working_position = PositionSnapshot.from_net(pos)
        self.close_leg_queue.clear()
        self.order_offsets.clear()
        self.pending_trades.clear()
        self.submit_inflight = False
        self._deferred_follow = OrderAction()
        self.mismatch_grace = 0
        self.trade_ledger.reset_memory()

    def stop(self) -> None:
        """停止交易，状态机复位；不清除持仓记账。"""
        self.machine.reset()
        self.active_orders.clear()
        self._pending_follow = OrderAction()
        self.recovery.trading_enabled = False
        self.recovery.system_state = SystemState.STARTING
        self.last_message = "策略停止"
        self.was_disconnected = False
        self.bars_since_reconcile = 0
        self.close_leg_queue.clear()
        self.order_offsets.clear()
        self.pending_trades.clear()
        self.submit_inflight = False
        self._deferred_follow = OrderAction()
        self.mismatch_grace = 0
        self.trade_ledger.close()

    def set_exchange(self, exchange) -> str:
        self.exchange = normalize_exchange(exchange)
        return self.exchange

    def set_working_position(self, snapshot: PositionSnapshot) -> None:
        """测试或外部注入今昨仓。"""
        snap = copy_snapshot(snapshot)
        self.working_position = snap
        self.position_sync.apply_snapshot(snap)
        self.last_position_snapshot = snap

    # ---------- 两阶段同步 ----------

    def sync_position(self, snapshot: PositionSnapshot, query_ok: bool = True) -> bool:
        """第一阶段：写入账户仓。成功后状态变为 SYNCING_ORDERS。"""
        self.recovery.system_state = SystemState.SYNCING_POSITION
        self.recovery.trading_enabled = False
        if not query_ok:
            self.recovery.enter_error("持仓查询失败", RecoveryReason.QUERY_FAILED)
            self.last_message = self.recovery.last_message
            return False
        self.position_sync.apply_snapshot(snapshot)
        self.recovery.system_state = SystemState.SYNCING_ORDERS
        self.last_message = f"持仓已同步 account_pos={snapshot.net}，开始同步活动订单"
        self.working_position = copy_snapshot(snapshot)
        self.close_leg_queue.clear()
        return True

    def sync_orders(self,orders: Optional[Iterable[OrderSnapshot]] = None, query_ok: bool = True) -> SystemState:
        """
        第二阶段：登记活动委托并对账。
        READY 时把 strategy_pos / target_pos 对齐账户，避免启动立刻追仓。
        """
        if self.recovery.system_state == SystemState.ERROR:
            return self.system_state

        if not query_ok:
            self.recovery.enter_error("活动订单查询失败", RecoveryReason.QUERY_FAILED)
            self.last_message = self.recovery.last_message
            return self.system_state

        if not self.position_sync.synced:
            self.recovery.enter_error("持仓尚未同步，不能对账订单")
            self.last_message = self.recovery.last_message
            return self.system_state

        snapshots = [item for item in (orders or []) if item.active]
        self.active_orders = {item.vt_orderid: item for item in snapshots}
        self.machine.reset()
        if snapshots:
            # 已成部分已在账户里：策略仓对齐账户，applied=traded，避免剩余成交被多扣
            self.strategy_pos = self.account_pos
            self.target_pos = self.account_pos
            for item in snapshots:
                self._bind_existing_order(item)
                self.machine.register_order(item.vt_orderid, item.intent, self.target_pos)

        report = self.position_sync.build_report(
            strategy_pos=self.strategy_pos,
            account_pos=self.account_pos,
            active_orders=snapshots,
            query_ok=True,
            position_query_ok=True,
            order_query_ok=True,
        )
        state = self.recovery.apply_sync(report, adopt_mismatch=self.adopt_account_on_mismatch)

        if state == SystemState.READY:
            if self.adopt_account_on_mismatch or report.matched:
                self.strategy_pos = self.account_pos
            self.target_pos = self.account_pos
            self.machine.reset()
            self.skip_next_send = False
            self.reject_count = 0
        elif state == SystemState.RECOVERING and snapshots:
            detail = self.hanging_order_text
            self.last_message = (
                f"{self.recovery.last_message}；已挂接 {detail}；"
                f"strategy_pos=account_pos={self.strategy_pos}。"
                "等待该单成交，或撤单后再 adopt"
            )
            self.recovery.last_message = self.last_message
            return state

        self.last_message = self.recovery.last_message
        return state

    def _bind_existing_order(self, item: OrderSnapshot) -> None:
        traded = max(0, int(item.traded))
        volume = max(int(item.volume), traded)
        item.volume = volume
        item.traded = traded
        item.applied = min(traded, volume)
        complete_order_snapshot(item)
        offset = item.offset
        if item.intent in (OrderIntent.CLOSE_LONG, OrderIntent.CLOSE_SHORT) and offset == OffsetKind.OPEN:
            offset = OffsetKind.CLOSE
        if item.intent in (OrderIntent.OPEN_LONG, OrderIntent.OPEN_SHORT):
            offset = OffsetKind.OPEN
        item.offset = offset
        if item.direction == DirectionKind.UNKNOWN:
            item.direction = direction_from_intent(item.intent)
        self.order_offsets[item.vt_orderid] = offset

    @property
    def hanging_order_text(self) -> str:
        parts = [
            f"{order.vt_orderid} {order.direction.value} {order.offset.value} "
            f"{order.intent.value} {order.traded}/{order.volume} @{order.price}"
            for order in self._live_active_orders()
        ]
        return "; ".join(parts) if parts else ""

    def _maybe_release_boot_recovery(self) -> None:
        if self.system_state != SystemState.RECOVERING:
            return
        if self.recovery_reason != RecoveryReason.ACTIVE_ORDERS:
            return
        if self._live_active_orders():
            return
        self.target_pos = int(self.strategy_pos)
        self.machine.reset()
        self.close_leg_queue.clear()
        self._pending_follow = OrderAction()
        self.recovery.force_ready(
            f"启动挂单已结束，target_pos={self.target_pos}，等待新信号"
        )
        self.last_message = self.recovery.last_message

    def adopt_account_position(self) -> bool:
        """人工确认：用账户仓覆盖策略仓。仍有挂单则拒绝。"""
        if not self.position_sync.synced:
            self.recovery.enter_error("人工确认失败：持仓尚未同步")
            self.last_message = self.recovery.last_message
            return False
        if self._live_active_orders():
            self.recovery.enter_recovering(
                RecoveryReason.ACTIVE_ORDERS,
                "仍有活动订单，不能确认恢复；请等待成交或先撤单",
            )
            self.last_message = self.recovery.last_message
            return False
        self.strategy_pos = self.recovery.confirm_adopt_account(self.account_pos)
        self.target_pos = self.account_pos
        self.machine.reset()
        self.skip_next_send = False
        self.reject_count = 0
        self.last_message = self.recovery.last_message
        return True

    def set_target_pos(self, target: int) -> None:
        """策略信号改目标仓。目标变化时解除「拒单暂停一次」。"""
        target = int(target)
        if target != self.target_pos:
            self.skip_next_send = False
        self.target_pos = target

    def note_bar(self) -> None:
        """新 K 线：解除「拒单后暂停一次」。"""
        self.skip_next_send = False

    # ---------- 交易许可 ----------

    def system_allows_trade(self) -> bool: # 系统是否允许交易
        if not self.gateway.connected:
            return False
        if self.system_state == SystemState.DISCONNECTED:
            return False
        return (
            self.safety_fuse
            and self.position_sync.synced
            and self.recovery.can_trade()
        )

    def begin_submit(self) -> None:
        """buy()/send_order() 调用前：禁止重入再发。"""
        self.submit_inflight = True
        self._deferred_follow = OrderAction()

    def end_submit(self) -> None:
        self.submit_inflight = False

    def defer_follow(self, action: OrderAction) -> None:
        if action.should_send:
            self._deferred_follow = action

    def take_deferred_follow(self) -> OrderAction:
        if self._deferred_follow.should_send:
            follow = self._deferred_follow
            self._deferred_follow = OrderAction()
            return follow
        return self._take_follow()

    def can_send_new_order(self) -> bool: # 是否可以发送新订单
        return (
            self.system_allows_trade()
            and not self.submit_inflight
            and not self._live_active_orders()
            and not self.skip_next_send
        )

    def can_send_follow_order(self) -> bool: # 是否可以发送跟随订单
        return (
            self.system_allows_trade()
            and not self.submit_inflight
            and not self._live_active_orders()
        )

    def _live_active_orders(self) -> List[OrderSnapshot]: # 获取活动订单
        return [order for order in self.active_orders.values() if order.active]

    # ---------- Gateway / 周期对账 ----------

    def on_gateway_status(self, td_ok: bool, md_ok: bool) -> str:
        """
        根据交易/行情通道状态切换连接。
        返回：
            ""          无变化
            "lost"      刚断开，已禁止下单
            "restored"  刚恢复，调用方必须立刻做两阶段同步
        """
        flags = GatewayFlags(bool(td_ok), bool(md_ok))
        self.gateway = flags

        if not flags.connected:
            if self.system_state != SystemState.DISCONNECTED:
                self.was_disconnected = True
                self.recovery.on_disconnect()
                self.last_message = (
                    f"Gateway 断开：td={flags.td_ok}, md={flags.md_ok}，暂停下单"
                )
                return "lost"
            return ""

        if self.was_disconnected or self.system_state == SystemState.DISCONNECTED:
            self.was_disconnected = False
            self.recovery.on_reconnect()
            self.position_sync.reset()
            self.last_message = "Gateway 已重连，开始重新同步持仓和活动订单"
            return "restored"

        return ""

    def should_reconcile_on_bar(self) -> bool:
        """是否在本根 K 线查询一次账户持仓（事件总线的兜底）。"""
        if self.reconcile_every_bars <= 0:
            return False
        self.bars_since_reconcile += 1
        if self.bars_since_reconcile >= self.reconcile_every_bars:
            self.bars_since_reconcile = 0
            return True
        return False

    def _busy_closing(self) -> bool:
        """有活动单或还有未发平仓腿：本地今昨正在随成交推进，账户缓存可能落后。"""
        return bool(self._live_active_orders() or self.close_leg_queue)

    def _adopt_working(self, snapshot: PositionSnapshot) -> None:
        self.working_position = copy_snapshot(snapshot)

    def apply_position_update(
        self,
        snapshot: PositionSnapshot,
        query_ok: bool = True,
    ) -> ReconcileResult:
        """
        处理 PositionData / 主动查询得到的账户持仓。

        不走 sync_position()，因此不会打回 SYNCING、不改 target_pos。
        working_position 只在空闲且净仓已对齐时用账户今昨校准，避免旧缓存盖掉刚成交的本地账。
        """
        strategy_pos = int(self.strategy_pos)

        if not query_ok:
            result = ReconcileResult(
                action="skipped",
                reason="周期对账：持仓查询失败，忽略本轮",
                account_pos=self.account_pos,
                strategy_pos=strategy_pos,
            )
            self.last_message = result.reason
            return result

        if self.system_state in (
            SystemState.STARTING,
            SystemState.SYNCING_POSITION,
            SystemState.SYNCING_ORDERS,
            SystemState.DISCONNECTED,
            SystemState.ERROR,
        ):
            if self.system_state in (
                SystemState.SYNCING_POSITION,
                SystemState.SYNCING_ORDERS,
            ):
                self.position_sync.apply_snapshot(snapshot)
                self.last_position_snapshot = snapshot
                self._adopt_working(snapshot)
            result = ReconcileResult(
                action="skipped",
                reason=f"周期对账：当前状态 {self.system_state.value}，不在此比对",
                account_pos=snapshot.net,
                strategy_pos=strategy_pos,
            )
            self.last_message = result.reason
            return result

        self.position_sync.apply_snapshot(snapshot)
        self.last_position_snapshot = snapshot
        account_pos = snapshot.net

        if self._busy_closing():
            self.mismatch_grace = 0
            result = ReconcileResult(
                action="skipped",
                reason="周期对账：存在活动订单或未发平仓腿，不覆盖今昨、不对账",
                account_pos=account_pos,
                strategy_pos=strategy_pos,
            )
            self.last_message = result.reason
            return result

        if strategy_pos != account_pos:
            self.mismatch_grace += 1
            if self.mismatch_grace < self.mismatch_grace_limit:
                result = ReconcileResult(
                    action="skipped",
                    reason=(
                        f"周期对账：净仓暂未对齐 "
                        f"(strategy_pos={strategy_pos}, account_pos={account_pos})，"
                        f"视为缓存滞后 {self.mismatch_grace}/{self.mismatch_grace_limit}"
                    ),
                    account_pos=account_pos,
                    strategy_pos=strategy_pos,
                )
                self.last_message = result.reason
                return result
            self.recovery.enter_recovering(
                RecoveryReason.POSITION_MISMATCH,
                f"周期对账不一致：strategy_pos={strategy_pos}, account_pos={account_pos}",
            )
            result = ReconcileResult(
                action="mismatch",
                reason=self.recovery.last_message,
                account_pos=account_pos,
                strategy_pos=strategy_pos,
            )
            self.last_message = result.reason
            return result

        self.mismatch_grace = 0
        self._adopt_working(snapshot)

        if (
            self.system_state == SystemState.RECOVERING
            and self.recovery_reason in (
                RecoveryReason.POSITION_MISMATCH,
                RecoveryReason.ACTIVE_ORDERS,
            )
            and not self._live_active_orders()
        ):
            self.target_pos = strategy_pos
            self.machine.reset()
            self.recovery.force_ready("周期对账已一致，恢复交易")
            result = ReconcileResult(
                action="recovered",
                reason=self.recovery.last_message,
                account_pos=account_pos,
                strategy_pos=strategy_pos,
            )
            self.last_message = result.reason
            return result

        result = ReconcileResult(
            action="matched",
            reason=f"周期对账一致：pos={strategy_pos}",
            account_pos=account_pos,
            strategy_pos=strategy_pos,
        )
        self.last_message = result.reason
        return result

    # ---------- 决策 / 下单登记 ----------

    def _plan_snapshot(self) -> PositionSnapshot: # 获取当前工作仓位
        return normalize_td_yd(self.working_position) # 规范化今昨持仓

    def decide(self) -> OrderAction: # 决策
        """
        先看平仓腿队列：有未发腿且当前无活动单 → 发队首。
        否则走净仓决策，平仓动作再按交易所拆成腿写入队列。
        """
        #1. 检查“拒单暂停”状态(如果跳过下一轮发送且没有活动订单，则返回拒单原因)
        if self.skip_next_send and not self.machine.has_active_orders(): 
            return OrderAction(reason="拒单后暂停一次，本轮不重复发单")
        # 2. 检查平仓腿队列: 有未发腿且当前无活动单 → 发队首。
        if self.close_leg_queue and not self._live_active_orders(): 
            return self.close_leg_queue[0]

        #3. 调用状态机决策: 决策结果分 4 种情况处理
        action = self.machine.decide(self.strategy_pos, self.target_pos) 
        # 3.1 处理取消所有订单的情况
        if action.kind == ActionKind.CANCEL_ALL:
            self.close_leg_queue.clear()
            return action
        # 3.2 处理不应发送的情况
        if not action.should_send:
            return action

        # 3.3 处理开仓情况: 清空平仓队列
        if action.kind in (ActionKind.OPEN_LONG, ActionKind.OPEN_SHORT):
            self.close_leg_queue.clear()
            return action

        # 3.4 处理平仓情况: 清空平仓队列，返回包装后的平仓动作
        snapshot = self._plan_snapshot() # 获取当前工作仓位
        if action.kind == ActionKind.CLOSE_LONG:
            # 3.4.1 处理平多仓情况: 生成平多仓腿
            legs = plan_close_long(
                action.volume,
                snapshot,
                self.exchange,
                next_state=action.next_state,
                pending_target=action.pending_target,
            )
        elif action.kind == ActionKind.CLOSE_SHORT:
            # 3.4.2 处理平空仓情况: 生成平空仓腿
            legs = plan_close_short(
                action.volume,
                snapshot,
                self.exchange,
                next_state=action.next_state,
                pending_target=action.pending_target,
            )
        else:
            return action

        if not legs:
            # 3.4.4 处理今昨仓不足的情况: 返回包装后的订单动作
            return OrderAction(
                reason=f"今昨仓不足，无法平仓：请求 {action.volume} 手，"
                f"多={snapshot.long_volume}(今{snapshot.long_today}/昨{snapshot.long_yesterday})，"
                f"空={snapshot.short_volume}(今{snapshot.short_today}/昨{snapshot.short_yesterday})"
            )

        if action.reason:
            # 3.4.5 处理有原因的情况: 合并原因
            legs[0].reason = f"{action.reason}；{legs[0].reason}"
            for leg in legs[1:]:
                leg.next_state = action.next_state
                leg.pending_target = action.pending_target

        self.close_leg_queue = list(legs) # 更新平仓队列
        return self.close_leg_queue[0]

    def register_sent_order(
        self,
        vt_orderid: str,
        intent: OrderIntent,
        volume: int,
        status: OrderStatus = OrderStatus.NOTTRADED,
        offset: Optional[OffsetKind] = None,
        consume_leg: bool = True,
        vt_symbol: str = "",
        direction: Optional[DirectionKind] = None,
        price: float = 0.0,
    ) -> None:
        """登记发出的委托，并记下柜台属性；队首腿手数匹配时才出队。"""
        existing = self.active_orders.get(vt_orderid) or self.order_history.get(vt_orderid)
        snapshot = OrderSnapshot(
            vt_orderid=vt_orderid,
            intent=intent,
            volume=int(volume),
            traded=0,
            applied=0,
            status=status,
            vt_symbol=vt_symbol or self.vt_symbol,
            direction=direction or DirectionKind.UNKNOWN,
            price=float(price or 0.0),
        )
        if existing:
            snapshot.applied = int(existing.applied)
            snapshot.traded = max(int(existing.traded), int(existing.applied))
            snapshot.volume = max(int(existing.volume), int(volume))
            snapshot.vt_symbol = snapshot.vt_symbol or existing.vt_symbol
            if snapshot.direction == DirectionKind.UNKNOWN:
                snapshot.direction = existing.direction
            if snapshot.price <= 0:
                snapshot.price = existing.price
            if existing.status in (
                OrderStatus.ALLTRADED,
                OrderStatus.CANCELLED,
                OrderStatus.REJECTED,
                OrderStatus.PARTTRADED,
            ):
                snapshot.status = existing.status

        if offset is None:
            if existing is not None:
                offset = existing.offset
            elif intent in (OrderIntent.OPEN_LONG, OrderIntent.OPEN_SHORT):
                offset = OffsetKind.OPEN
            elif self.close_leg_queue:
                offset = self.close_leg_queue[0].offset
            else:
                offset = OffsetKind.CLOSE
        snapshot.offset = offset
        complete_order_snapshot(snapshot)

        if snapshot.status in (OrderStatus.ALLTRADED, OrderStatus.CANCELLED, OrderStatus.REJECTED):
            self.active_orders.pop(vt_orderid, None)
            self.order_history[vt_orderid] = snapshot
        else:
            self.active_orders[vt_orderid] = snapshot
            self.machine.register_order(vt_orderid, intent, self.target_pos)
            self.recovery.mark_trading()

        self.order_offsets[vt_orderid] = snapshot.offset

        if consume_leg and self.close_leg_queue:
            head = self.close_leg_queue[0]
            if INTENT_TO_KIND.get(intent) == head.kind and int(head.volume) == int(volume):
                self.close_leg_queue.pop(0)

        self._flush_pending_trades(vt_orderid)
        stored = self.active_orders.get(vt_orderid) or self.order_history.get(vt_orderid)
        if stored and stored.intent != OrderIntent.UNKNOWN and stored.traded > stored.applied:
            booked = self._book_traded_delta(stored)
            if booked.accepted:
                self._apply_working_position(vt_orderid, booked.applied_volume)

    def register_sent_orders(
        self,
        vt_orderids,
        intent: OrderIntent,
        volume: int,
        offset: Optional[OffsetKind] = None,
        vt_symbol: str = "",
        price: float = 0.0,
    ) -> bool:
        """同一意图拆成多个 vt_orderid 时，只让第一笔消费平仓腿队列。"""
        orderids = _as_vt_orderids(vt_orderids) # 将订单ID列表转换为字符串列表(规范化)
        if not orderids: # 如果订单ID列表为空
            self.machine.mark_idle()
            self.last_message = f"订单发送失败：intent={intent.value}"
            return False
        first = True
        for vt_orderid in orderids:
            self.register_sent_order(
                vt_orderid, # 订单ID
                intent, # 意图
                volume, # 数量
                offset=offset, # 交易类型
                consume_leg=first, # 第一笔订单消费平仓腿队列
                vt_symbol=vt_symbol,
                price=price,
            )
            first = False
        return True

    def _take_follow(self) -> OrderAction:
        follow = self._pending_follow
        self._pending_follow = OrderAction()
        return follow

    def _commit_fill(self, order: OrderSnapshot, volume: int) -> TradeApplyResult:
        """
        把手数计入 strategy_pos。只增加 applied，不把柜台 traded 当成已经入过账。
        on_order / on_trade 都走这里，避免先委托回报后成交回报时漏记或记两次。
        """
        unapplied = order.unapplied # 未成交数量
        take = min(max(0, int(volume)), unapplied) # 成交数量
        # 如果成交数量小于等于0，则返回结果
        if take <= 0:
            return TradeApplyResult(False, True, f"订单已无剩余量: {order.vt_orderid}")

        overfill = int(volume) > take # 是否超量成交

        order.applied += take # 更新已成交数量
        order.traded = max(int(order.traded), int(order.applied)) # 更新成交数量
        self.strategy_pos = _apply_intent(self.strategy_pos, order.intent, take) # 更新策略持仓
        self.reject_count = 0 # 重置拒单计数

        if order.applied >= order.volume:
            if order.status not in (OrderStatus.CANCELLED, OrderStatus.REJECTED):
                order.status = OrderStatus.ALLTRADED
            self.active_orders.pop(order.vt_orderid, None)
            self.order_history[order.vt_orderid] = order
            self.machine.finish_order(order.vt_orderid)
        elif order.status in (OrderStatus.CANCELLED, OrderStatus.REJECTED):
            self.active_orders.pop(order.vt_orderid, None)
            self.order_history[order.vt_orderid] = order
            self.machine.finish_order(order.vt_orderid)
        else:
            order.status = OrderStatus.PARTTRADED
            self.active_orders[order.vt_orderid] = order

        follow = self.machine.on_trade(self.strategy_pos, self.target_pos)
        self._pending_follow = follow
        if self.strategy_pos == self.target_pos and not self._live_active_orders():
            self.recovery.mark_ready_idle()

        reason = f"成交计入 {take} 手，strategy_pos={self.strategy_pos}"

        if overfill:
            reason += f"；超出委托的 {int(volume) - take} 手已丢弃"

        self._maybe_release_boot_recovery()

        return TradeApplyResult(
            accepted=True,
            ignored=False,
            reason=reason,
            follow=follow,
            applied_volume=take,
        )


    def _book_traded_delta(self, order: OrderSnapshot) -> TradeApplyResult:
        """委托回报里 traded 增加时，把差额记入策略仓。拒单不入账。"""
        if order.status in (OrderStatus.REJECTED, OrderStatus.UNKNOWN):
            return TradeApplyResult(False, True, f"委托状态 {order.status.value}")
        if order.intent == OrderIntent.UNKNOWN:
            return TradeApplyResult(False, True, f"意图未知，暂不按委托回报入账: {order.vt_orderid}")
        reported = min(max(0, int(order.traded)), max(0, int(order.volume)))
        delta = reported - int(order.applied)
        if delta <= 0:
            return TradeApplyResult(False, True, f"委托状态 {order.status.value}")
        return self._commit_fill(order, delta)

    # ---------- 订单回报 ----------

    def _apply_order_report(self, order: OrderSnapshot) -> TradeApplyResult:
        previous = self.active_orders.get(order.vt_orderid) or self.order_history.get(order.vt_orderid)
        if previous:
            if order.intent == OrderIntent.UNKNOWN:
                order.intent = previous.intent
            if order.volume <= 0:
                order.volume = previous.volume
            if order.traded < previous.traded:
                order.traded = previous.traded
            order.applied = int(previous.applied)
            order.vt_symbol = order.vt_symbol or previous.vt_symbol
            if order.direction == DirectionKind.UNKNOWN:
                order.direction = previous.direction
            if order.price <= 0:
                order.price = previous.price
            if (
                order.offset == OffsetKind.OPEN
                and previous.offset != OffsetKind.OPEN
            ):
                order.offset = previous.offset
        complete_order_snapshot(order)

        result = self._book_traded_delta(order)
        if result.accepted:
            result.follow = self._take_follow()

        if order.active:
            self.active_orders[order.vt_orderid] = order
            if order.vt_orderid not in self.machine.active_orderids:
                self.machine.register_order(order.vt_orderid, order.intent, self.target_pos)
        else:
            self.active_orders.pop(order.vt_orderid, None)
            self.order_history[order.vt_orderid] = order
            self.machine.finish_order(order.vt_orderid)

        if order.status == OrderStatus.UNKNOWN:
            self.recovery.enter_recovering(
                RecoveryReason.ORDER_UNKNOWN,
                f"订单状态未知: {order.vt_orderid}",
            )
            self.last_message = self.recovery.last_message
            return result

        if order.status == OrderStatus.REJECTED:
            self.machine.on_rejected()
            self.skip_next_send = True
            self.reject_count += 1
            self.last_message = f"订单被拒绝: {order.vt_orderid}"
            if self.reject_count >= 3:
                self.recovery.enter_recovering(
                    RecoveryReason.ORDER_REJECTED,
                    f"连续拒单 {self.reject_count} 次，暂停自动交易",
                )
                self.last_message = self.recovery.last_message
            return result

        if order.status == OrderStatus.CANCELLED:
            self.machine.on_cancelled()
            extra = f"订单已撤销: {order.vt_orderid} traded={order.traded}"
            self.last_message = f"{result.reason}；{extra}" if result.accepted else extra
            return result

        if order.status == OrderStatus.ALLTRADED:
            self.last_message = result.reason if result.accepted else f"订单全部成交: {order.vt_orderid}"
        elif result.accepted:
            self.last_message = result.reason
        return result

    def on_order(self, order) -> TradeApplyResult:
        """撤单/拒单后清空平仓腿。委托回报入账时同步今昨仓，并挂上下一平仓腿。"""
        result = self._apply_order_report(order)
        if result.accepted:
            self._apply_working_position(order.vt_orderid, result.applied_volume)
        flushed = self._flush_pending_trades(order.vt_orderid)
        if flushed.accepted:
            result = flushed
        self._maybe_release_boot_recovery()
        if order.status in (OrderStatus.CANCELLED, OrderStatus.REJECTED):
            self.close_leg_queue.clear()
            extra = "已清空平仓腿队列，将按当前今昨仓重算"
            self.last_message = f"{self.last_message}；{extra}" if self.last_message else extra
            return result
        return self._attach_follow(result)

    def on_trade(self, vt_orderid: str, volume: int, tradeid: str = "") -> TradeApplyResult:
        """
        成交入账：按意图改 strategy_pos，超量部分丢弃。
        若 on_order 已经按 traded 入过账，这里不再加第二次。
        找不到委托时先暂存，等登记 / on_order 再补。
        """
        result = self._apply_trade_report(vt_orderid, volume, tradeid)
        if result.accepted:
            self._apply_working_position(vt_orderid, result.applied_volume)
        return self._attach_follow(result)

    def _store_pending_trade(self, vt_orderid: str, volume: int, tradeid: str) -> None:
        bucket = self.pending_trades.setdefault(vt_orderid, [])
        if tradeid and any(item[1] == tradeid for item in bucket):
            return
        bucket.append((int(volume), str(tradeid)))
        total = sum(len(items) for items in self.pending_trades.values())
        while total > self.pending_trade_limit and self.pending_trades:
            oldest_id = next(iter(self.pending_trades))
            self.pending_trades[oldest_id].pop(0)
            if not self.pending_trades[oldest_id]:
                self.pending_trades.pop(oldest_id, None)
            total -= 1

    def _flush_pending_trades(self, vt_orderid: str) -> TradeApplyResult:
        items = self.pending_trades.pop(vt_orderid, [])
        if not items:
            return TradeApplyResult(False, True, "")
        order = self.active_orders.get(vt_orderid) or self.order_history.get(vt_orderid)
        if order is None or order.intent == OrderIntent.UNKNOWN:
            self.pending_trades[vt_orderid] = items
            return TradeApplyResult(False, True, f"成交仍在暂存: {vt_orderid}")

        last = TradeApplyResult(False, True, "")
        for volume, tradeid in items:
            last = self._book_known_trade(order, volume, tradeid)
            if last.accepted:
                self._apply_working_position(vt_orderid, last.applied_volume)
            order = self.active_orders.get(vt_orderid) or self.order_history.get(vt_orderid) or order
        return last

    def _book_known_trade(self, order: OrderSnapshot, volume: int, tradeid: str) -> TradeApplyResult:
        if self.trade_ledger.seen(tradeid):
            return TradeApplyResult(
                False, True, f"重复成交回报: {tradeid}", follow=self._take_follow()
            )
        if order.intent == OrderIntent.UNKNOWN:
            return TradeApplyResult(False, True, f"意图未知，暂不入账: {order.vt_orderid}")
        if order.unapplied <= 0:
            self.trade_ledger.remember(tradeid, order.vt_orderid, volume)
            return TradeApplyResult(
                False,
                True,
                f"已由委托回报入账，不再重复加仓: {order.vt_orderid}",
                follow=self._take_follow(),
            )
        result = self._commit_fill(order, min(int(volume), order.unapplied))
        result.follow = self._take_follow()
        self.trade_ledger.remember(tradeid, order.vt_orderid, result.applied_volume or volume)
        self.last_message = result.reason
        return result

    def _apply_trade_report(self, vt_orderid: str, volume: int, tradeid: str = "") -> TradeApplyResult:
        if self.trade_ledger.seen(tradeid):
            return TradeApplyResult(
                False, True, f"重复成交回报: {tradeid}", follow=self._take_follow()
            )
        if volume <= 0:
            return TradeApplyResult(False, True, "成交数量必须 > 0")

        order = self.active_orders.get(vt_orderid) or self.order_history.get(vt_orderid)
        if order is None or order.intent == OrderIntent.UNKNOWN:
            self._store_pending_trade(vt_orderid, volume, tradeid)
            self.last_message = f"成交暂存，等待委托登记: {vt_orderid}"
            return TradeApplyResult(False, True, self.last_message)

        return self._book_known_trade(order, volume, tradeid)

    # 处理成交后的后续动作
    def _attach_follow(self, result: TradeApplyResult) -> TradeApplyResult:
        already_booked = "已由委托回报入账" in (result.reason or "")
        # 如果结果未接受且未由委托回报入账且不需要发送，则返回结果
        if not result.accepted and not already_booked and not result.follow.should_send:
            return result

        if self.close_leg_queue and not self._live_active_orders():
            nxt = self.close_leg_queue[0]
            result.follow = nxt
            result.reason = f"{result.reason}；下一腿 {nxt.offset.value} {nxt.volume} 手"
            return result

        if result.follow.should_send and result.follow.kind in (
            ActionKind.OPEN_LONG,
            ActionKind.OPEN_SHORT,
        ):
            if self.close_leg_queue:
                result.follow = OrderAction(reason="仍有平今/平昨腿未完成，暂不开反向仓")
            else:
                result.follow.offset = OffsetKind.OPEN
        return result

    def _apply_working_position(self, vt_orderid: str, volume: int) -> None:
        """按委托意图和 offset 更新本地今昨，使下一腿规划不用等账户推送。"""
        snap = normalize_td_yd(self._plan_snapshot())
        order = self.active_orders.get(vt_orderid) or self.order_history.get(vt_orderid)
        offset = self.order_offsets.get(vt_orderid)
        if offset is None:
            offset = order.offset if order is not None else OffsetKind.OPEN
        intent = order.intent if order else OrderIntent.UNKNOWN

        if intent == OrderIntent.OPEN_LONG:
            snap.long_today += volume
            snap.long_volume = snap.long_today + snap.long_yesterday
        elif intent == OrderIntent.OPEN_SHORT:
            snap.short_today += volume
            snap.short_volume = snap.short_today + snap.short_yesterday
        elif intent == OrderIntent.CLOSE_LONG:
            snap.long_today, snap.long_yesterday = _consume_side(
                snap.long_today, snap.long_yesterday, volume, offset
            )
            snap.long_volume = snap.long_today + snap.long_yesterday
        elif intent == OrderIntent.CLOSE_SHORT:
            snap.short_today, snap.short_yesterday = _consume_side(
                snap.short_today, snap.short_yesterday, volume, offset
            )
            snap.short_volume = snap.short_today + snap.short_yesterday

        self.working_position = snap
