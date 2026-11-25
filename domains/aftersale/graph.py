"""售后域图：紧凑 ReAct 循环（同 merchandising/policy 模板）。

fake 决策：工单号/报修建单 → query/create；责任类 → reason；默认 → eligibility。
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

from domains.aftersale.tools import TOOL_TO_SKILL, TOOLS


class DomainState(TypedDict):
    messages: Annotated[list, add_messages]
    artifact: dict
    summary: str


_SKU_RE = re.compile(r"SKU[-A-Za-z0-9]*", re.IGNORECASE)
_ORDER_RE = re.compile(r"SO-\d+", re.IGNORECASE)
_TICKET_RE = re.compile(r"TK-\d+", re.IGNORECASE)
_FAULTS = ("失灵", "进水", "摔", "闪烁", "损坏", "故障", "坏了", "不出声", "断连")


def _fault_of(text: str) -> str:
    return next((w for w in _FAULTS if w in text), "故障")


def _fake_decide(messages: list) -> AIMessage:
    last = messages[-1]
    if isinstance(last, ToolMessage):
        return AIMessage(content="")
    text = " ".join(m.content for m in messages if isinstance(m.content, str))
    tm = _TICKET_RE.search(text)
    if tm and not any(w in text for w in ("建", "报修", "创建", "申请")):
        return AIMessage(
            content="", tool_calls=[{
                "name": "query_ticket", "args": {"ticket_no": tm.group(0).upper()},
                "id": "call_1"}])
    if any(w in text for w in ("建", "报修", "创建", "申请")) and "工单" in text:
        sku = (_SKU_RE.search(text) or [None]) and (
            _SKU_RE.search(text).group(0).upper() if _SKU_RE.search(text) else "")
        return AIMessage(
            content="", tool_calls=[{
                "name": "create_ticket",
                "args": {"sku": sku or "SKU-A001", "fault_type": _fault_of(text)},
                "id": "call_1"}])
    if any(w in text for w in ("责任", "谁的", "谁承担", "运费谁")):
        return AIMessage(
            content="", tool_calls=[{
                "name": "determine_responsibility",
                "args": {"fault_type": _fault_of(text)}, "id": "call_1"}])
    sku_m = _SKU_RE.search(text)
    sku = sku_m.group(0).upper() if sku_m else "SKU-A001"
    om = _ORDER_RE.search(text)
    return AIMessage(
        content="", tool_calls=[{
            "name": "judge_eligibility",
            "args": {"sku": sku, "order_no": om.group(0).upper() if om else ""},
            "id": "call_1"}])


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
        "authority_domain": "aftersale",
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
    g.add_node("tools", audited_tool_node(TOOLS, "aftersale", TOOL_TO_SKILL))
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
        if "eligible" in data:
            summary = (
                f"{data['sku']}（购买于 {data['buy_date']}，已 {data['days_since_purchase']} 天）："
                f"{data['conclusion']}。依据：{data['clause']}"
                + ("（降级：政策服务不可用）" if data.get("degraded") else "")
            )
        elif "responsibility" in data:
            summary = (
                f"「{data['fault_type']}」责任判定：{data['detail']}。"
                f"运费依据：{data['freight_clause']}"
            )
        elif "ticket_no" in data and data.get("status") == "created":
            summary = f"工单已创建：{data['ticket_no']}（{data['sku']}，{data['fault_type']}）。"
        elif "ticket_no" in data:
            summary = f"工单 {data['ticket_no']} 当前状态：{data.get('status')}"
        elif data.get("error"):
            summary = f"查询失败：{data.get('hint') or data['error']}"
        else:
            summary = json.dumps(data, ensure_ascii=False)
    return artifact, summary
