"""
V2.3.1 第二刀：官方 CTP Gateway 的薄封装。

不修改 vnpy_ctp 安装目录里的 ctp_gateway.py。
继承官方类，只补：
- query_order / query_trade
- 未知 OrderSysID 的成交不 KeyError，建映射后重放

Station「系统 → 连接CTP」若已按官方类 add_gateway，
策略启动时会对现有 td_api 做 attach（实例补丁）。
若能在连接前 import 本模块，install() 会把模块上的 CtpGateway 换成封装类。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional


def orderid_from_ctp_order(data: dict) -> str:
    return f"{data.get('FrontID', 0)}_{data.get('SessionID', 0)}_{data.get('OrderRef', '')}"


def remember_sysid(mapping: dict, data: dict) -> None:
    sysid = str(data.get("OrderSysID") or "").strip()
    if sysid:
        mapping[sysid] = orderid_from_ctp_order(data)


def lookup_orderid(mapping: dict, data: dict) -> Optional[str]:
    sysid = str(data.get("OrderSysID") or "").strip()
    if not sysid:
        return None
    return mapping.get(sysid)


def _flush_pending_trades(td: Any, emit_trade: Callable) -> None:
    pending: List[dict] = getattr(td, "_wqk_pending_trades", None) or []
    if not pending:
        return
    mapping = getattr(td, "sysid_orderid_map", None)
    if not isinstance(mapping, dict):
        return
    kept: List[dict] = []
    for item in pending:
        if lookup_orderid(mapping, item):
            emit_trade(item)
        else:
            kept.append(item)
    td._wqk_pending_trades = kept


def attach_wqk_ctp(gateway: Any) -> bool:
    """给已创建的官方 CtpGateway 实例打补丁。可重复调用。"""
    td = getattr(gateway, "td_api", None)
    if td is None:
        return False
    if getattr(td, "_wqk_ctp_patched", False):
        return True

    if not isinstance(getattr(td, "sysid_orderid_map", None), dict):
        td.sysid_orderid_map = {}

    td._wqk_ctp_patched = True
    td._wqk_pending_trades = []
    td.order_query_done = True
    td.trade_query_done = True

    orig_order = td.onRtnOrder
    orig_trade = td.onRtnTrade

    def on_rtn_order(data: dict) -> None:
        if isinstance(data, dict):
            remember_sysid(td.sysid_orderid_map, data)
        orig_order(data)
        _flush_pending_trades(td, orig_trade)

    def on_rtn_trade(data: dict) -> None:
        if not getattr(td, "contract_inited", True):
            orig_trade(data)
            return
        if not isinstance(data, dict):
            orig_trade(data)
            return
        if lookup_orderid(td.sysid_orderid_map, data):
            orig_trade(data)
            return
        td._wqk_pending_trades.append(data)

    def query_order() -> bool:
        if not getattr(td, "login_status", False):
            return False
        func = getattr(td, "reqQryOrder", None)
        if not callable(func):
            return False
        td.reqid = int(getattr(td, "reqid", 0) or 0) + 1
        td.order_query_done = False
        func(
            {
                "BrokerID": getattr(td, "brokerid", "") or "",
                "InvestorID": getattr(td, "userid", "") or "",
            },
            td.reqid,
        )
        return True

    def query_trade() -> bool:
        if not getattr(td, "login_status", False):
            return False
        func = getattr(td, "reqQryTrade", None)
        if not callable(func):
            return False
        td.reqid = int(getattr(td, "reqid", 0) or 0) + 1
        td.trade_query_done = False
        func(
            {
                "BrokerID": getattr(td, "brokerid", "") or "",
                "InvestorID": getattr(td, "userid", "") or "",
            },
            td.reqid,
        )
        return True

    def on_rsp_qry_order(data: dict, error: dict, reqid: int, last: bool) -> None:
        if error and error.get("ErrorID"):
            writer = getattr(gateway, "write_error", None) or getattr(gateway, "write_log", None)
            if writer:
                writer(f"委托查询失败：{error}")
        if data:
            on_rtn_order(data)
        if last:
            td.order_query_done = True

    def on_rsp_qry_trade(data: dict, error: dict, reqid: int, last: bool) -> None:
        if error and error.get("ErrorID"):
            writer = getattr(gateway, "write_error", None) or getattr(gateway, "write_log", None)
            if writer:
                writer(f"成交查询失败：{error}")
        if data:
            on_rtn_trade(data)
        if last:
            td.trade_query_done = True

    td.onRtnOrder = on_rtn_order
    td.onRtnTrade = on_rtn_trade
    td.query_order = query_order
    td.query_trade = query_trade
    td.onRspQryOrder = on_rsp_qry_order
    td.onRspQryTrade = on_rsp_qry_trade
    gateway.query_order = query_order
    gateway.query_trade = query_trade
    return True


def request_order_query(gateway: Any) -> bool:
    td = getattr(gateway, "td_api", None)
    func = getattr(td, "query_order", None) if td is not None else None
    if not callable(func):
        return False
    return bool(func())


def order_query_finished(gateway: Any) -> bool:
    td = getattr(gateway, "td_api", None)
    return bool(getattr(td, "order_query_done", False)) if td is not None else False


def gateway_can_query_order(gateway: Any) -> bool:
    td = getattr(gateway, "td_api", None)
    if td is None:
        return False
    if not getattr(td, "login_status", False):
        return False
    return callable(getattr(td, "reqQryOrder", None) or getattr(td, "query_order", None))


def _build_wrapper_classes():
    """官方 vnpy_ctp 可用时才建子类，避免开发机缺包时整文件 import 失败。"""
    try:
        from vnpy_ctp.gateway.ctp_gateway import CtpGateway, CtpTdApi
    except Exception:
        return None, None

    class WqkCtpTdApi(CtpTdApi):
        def __init__(self, gateway: Any) -> None:
            super().__init__(gateway)
            self._wqk_pending_trades: List[dict] = []
            self.order_query_done: bool = True
            self.trade_query_done: bool = True

        def onRtnOrder(self, data: dict) -> None:
            if isinstance(data, dict):
                remember_sysid(self.sysid_orderid_map, data)
            super().onRtnOrder(data)
            parent_trade = super().onRtnTrade
            _flush_pending_trades(self, parent_trade)

        def onRtnTrade(self, data: dict) -> None:
            if not self.contract_inited:
                self.trade_data.append(data)
                return
            if not isinstance(data, dict) or lookup_orderid(self.sysid_orderid_map, data):
                super().onRtnTrade(data)
                return
            self._wqk_pending_trades.append(data)

        def query_order(self) -> bool:
            if not self.login_status:
                return False
            self.reqid += 1
            self.order_query_done = False
            self.reqQryOrder(
                {"BrokerID": self.brokerid, "InvestorID": self.userid},
                self.reqid,
            )
            return True

        def query_trade(self) -> bool:
            if not self.login_status:
                return False
            self.reqid += 1
            self.trade_query_done = False
            self.reqQryTrade(
                {"BrokerID": self.brokerid, "InvestorID": self.userid},
                self.reqid,
            )
            return True

        def onRspQryOrder(self, data: dict, error: dict, reqid: int, last: bool) -> None:
            if error and error.get("ErrorID"):
                self.gateway.write_error("委托查询失败", error)
            if data:
                self.onRtnOrder(data)
            if last:
                self.order_query_done = True
                self.gateway.write_log("委托查询完成")

        def onRspQryTrade(self, data: dict, error: dict, reqid: int, last: bool) -> None:
            if error and error.get("ErrorID"):
                self.gateway.write_error("成交查询失败", error)
            if data:
                self.onRtnTrade(data)
            if last:
                self.trade_query_done = True
                self.gateway.write_log("成交查询完成")

    class WqkCtpGateway(CtpGateway):
        default_name = "CTP"

        def __init__(self, event_engine, gateway_name: str) -> None:
            super().__init__(event_engine, gateway_name)
            self.td_api = WqkCtpTdApi(self)

        def query_order(self) -> None:
            self.td_api.query_order()

        def query_trade(self) -> None:
            self.td_api.query_trade()

    return WqkCtpGateway, WqkCtpTdApi


WqkCtpGateway, WqkCtpTdApi = _build_wrapper_classes()


def install() -> bool:
    """把内存里的官方 CtpGateway 换成封装类。不改磁盘文件。连接前调用才有效。"""
    if WqkCtpGateway is None:
        return False
    try:
        import vnpy_ctp
        import vnpy_ctp.gateway.ctp_gateway as ctp_mod
    except Exception:
        return False
    ctp_mod.CtpGateway = WqkCtpGateway
    try:
        import vnpy_ctp.gateway as gw_pkg
        gw_pkg.CtpGateway = WqkCtpGateway
    except Exception:
        pass
    if hasattr(vnpy_ctp, "CtpGateway"):
        vnpy_ctp.CtpGateway = WqkCtpGateway
    return True


install()
