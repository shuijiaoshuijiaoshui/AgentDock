"""订单域图：紧凑 ReAct 循环（同 merchandising 模板）。

fake 决策：SO- 订单号 → order_query；含"改地址/修改地址" → order_amend；默认 order_query。
"""

import json
import re
import uuid
from datetime import datetime, timezone
from typing import Annotated, Literal, TypedDict

from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

from shared.config import get_settings
from shared.llm import get_main_llm
from shared.llm_guard import sanitize_messages_for_llm
from shared.request_context import drain_tool_events
from shared.audit import audited_tool_node

from domains.order.tools import STATUS_NAMES, TOOL_TO_SKILL, TOOLS


class DomainState(TypedDict):
    messages: Annotated[list, add_messages]
    artifact: dict
    summary: str


_ORDER_RE = re.compile(r"SO-\d+", re.IGNORECASE)


def _fake_decide(messages: list) -> AIMessage:
    last = messages[-1]
    if isinstance(last, ToolMessage):
        return AIMessage(content="")
    text = " ".join(m.content for m in messages if isinstance(m.content, str))
    om = _ORDER_RE.search(text)
    order_no = om.group(0).upper() if om else ""
    if "取消" in text and "订单" in text:
        rm = re.search(r"原因[：:]\s*([^\s，。]+)", text)
        return AIMessage(
            content="", tool_calls=[{
                "name": "cancel_order",
                "args": {"order_no": order_no, "reason": rm.group(1) if rm else "用户申请"},
                "id": "call_1"}])
    if "恢复" in text and ("订单" in text or "biz" in text.lower()):
        km = re.search(r"补偿键[：:]\s*([\w:.\-]+)", text)
        return AIMessage(
            content="", tool_calls=[{
                "name": "restore_order",
                "args": {"biz_key": km.group(1) if km else ""},
                "id": "call_1"}])
    if any(w in text for w in ("改价", "调价", "降价", "改价格")):
        pm = re.search(r"(?:改价|调价|降价|改价格)[^\d]{0,8}(\d+(?:\.\d+)?)", text)
        return AIMessage(
            content="", tool_calls=[{
                "name": "order_amend",
                "args": {"order_no": order_no, "address": "",
                         "new_price": float(pm.group(1)) if pm else None},
                "id": "call_1"}])
    if any(w in text for w in ("改地址", "修改地址", "改收货", "换个地址")):
        addr = ""
        am = re.search(r"(?:改到|改为|改成)\s*([^\s，。]+)", text)
        if am:
            addr = am.group(1)
        return AIMessage(
            content="", tool_calls=[{
                "name": "order_amend",
                "args": {"order_no": order_no, "address": addr},
                "id": "call_1"}])
    return AIMessage(
        content="", tool_calls=[{
            "name": "order_query", "args": {"order_no": order_no}, "id": "call_1"}])


def _route_after_agent(state: DomainState) -> Literal["tools", "emit_artifact"]:
    last = state["messages"][-1]
    return "tools" if getattr(last, "tool_calls", None) else "emit_artifact"


def emit_artifact(state: DomainState, config: RunnableConfig) -> dict:
    tool_msgs = [m for m in state["messages"] if isinstance(m, ToolMessage)]
    data = {}
    if tool_msgs:
        try:
            data = json.loads(tool_msgs[-1].content)
        except json.JSONDecodeError:
            data = {"raw": tool_msgs[-1].content}
    tool_name = ""
    last_ai_with_calls = next(
        (m for m in reversed(state["messages"]) if getattr(m, "tool_calls", None)), None
    )
    if last_ai_with_calls:
        tool_name = last_ai_with_calls.tool_calls[-1]["name"]
    meta = (config.get("metadata") or {}) if config else {}
    artifact = {
        "authority_domain": "order",
        "skill": TOOL_TO_SKILL.get(tool_name, tool_name),
        "as_of": datetime.now(timezone.utc).isoformat(),
        "snapshot_id": meta.get("snapshot_id") or uuid.uuid4().hex[:12],
        "tool_events": drain_tool_events(),
        "data": data,
    }
    summary = ""
    for m in reversed(state["messages"]):
        if isinstance(m, AIMessage) and m.content:
            summary = m.content
            break
    return {"artifact": artifact, "summary": summary}


def build_domain_graph(checkpointer=None):  # noqa: ANN001
    def agent_node(state: DomainState, config: RunnableConfig) -> dict:
        if get_settings().llm_provider == "fake":
            return {"messages": [_fake_decide(state["messages"])]}
        llm = get_main_llm().bind_tools(TOOLS)
        return {"messages": [llm.invoke(sanitize_messages_for_llm(state["messages"]), config=config)]}

    g = StateGraph(DomainState)
    g.add_node("agent", agent_node)
    g.add_node("tools", audited_tool_node(TOOLS, "order", TOOL_TO_SKILL))
    g.add_node("emit_artifact", emit_artifact)
    g.add_edge(START, "agent")
    g.add_conditional_edges("agent", _route_after_agent,
                            {"tools": "tools", "emit_artifact": "emit_artifact"})
    g.add_edge("tools", "agent")
    g.add_edge("emit_artifact", END)
    return g.compile(checkpointer=checkpointer)


def artifact_builder(state: dict) -> tuple[dict, str]:
    artifact = state.get("artifact") or {}
    summary = state.get("summary") or ""
    data = artifact.get("data") or {}
    if not summary and isinstance(data, dict):
        if data.get("status") == "pending_confirm":
            p = data.get("preview") or {}
            summary = (
                f"即将把订单 {p.get('order_no')} 的收货地址由「{p.get('old_address')}」"
                f"改为「{p.get('new_address')}」。确认后才会执行。"
            )
        elif data.get("status") == "amended":
            summary = f"订单 {data['order_no']} 收货地址已修改为：{data['address']}。"
        elif "order_no" in data and "status_name" in data:
            summary = (
                f"订单 {data['order_no']}（{data.get('sku')} × {data.get('qty')}）"
                f"当前状态：{data['status_name']}，金额 {data.get('amount')} 元，"
                f"下单时间 {data.get('created_at')}，收货地址 {data.get('address')}。"
            )
        elif data.get("error"):
            summary = f"订单查询失败：{data.get('hint') or data['error']}"
        elif isinstance(data, list):
            summary = f"共查到 {len(data)} 笔订单。"
        else:
            summary = json.dumps(data, ensure_ascii=False)
    return artifact, summary
