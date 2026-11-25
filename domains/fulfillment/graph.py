"""履约域图：紧凑 ReAct 循环（同 merchandising 模板）。

fake 决策：SKU- 且含"库存/可售/能发/时效" → fulfill_atp；其余 → fulfill_track。
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

from domains.fulfillment.tools import TOOL_TO_SKILL, TOOLS


class DomainState(TypedDict):
    messages: Annotated[list, add_messages]
    artifact: dict
    summary: str


_ORDER_RE = re.compile(r"SO-\d+", re.IGNORECASE)
_SKU_RE = re.compile(r"SKU[-A-Za-z0-9]+", re.IGNORECASE)


def _fake_decide(messages: list) -> AIMessage:
    last = messages[-1]
    if isinstance(last, ToolMessage):
        return AIMessage(content="")
    text = " ".join(m.content for m in messages if isinstance(m.content, str))
    om = _ORDER_RE.search(text)
    if any(w in text for w in ("退货", "退库")):
        return AIMessage(
            content="", tool_calls=[{
                "name": "return_create",
                "args": {"order_no": om.group(0).upper() if om else "",
                         "reason": "客户退货"},
                "id": "call_1"}])
    sm = _SKU_RE.search(text)
    if sm and any(w in text for w in ("库存", "可售", "能发", "时效", "现货", "几天")):
        return AIMessage(
            content="", tool_calls=[{
                "name": "fulfill_atp", "args": {"sku": sm.group(0).upper()},
                "id": "call_1"}])
    om = _ORDER_RE.search(text)
    return AIMessage(
        content="", tool_calls=[{
            "name": "fulfill_track",
            "args": {"order_no": om.group(0).upper() if om else ""},
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
        "authority_domain": "fulfillment",
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
    g.add_node("tools", audited_tool_node(TOOLS, "fulfillment", TOOL_TO_SKILL))
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
        if "nodes" in data:
            nodes = data.get("nodes") or []
            last = nodes[-1] if nodes else {}
            summary = (
                f"订单 {data['order_no']} 由{data['carrier']}承运（运单 "
                f"{data['tracking_no']}），当前{data.get('status_name')}；"
                f"最新节点：{last.get('node_time', '')} {last.get('location', '')} "
                f"{last.get('description', '')}。"
            )
        elif "available" in data:
            summary = (
                f"{data['sku']} 可承诺库存 {data['available']}，"
                f"{data.get('hint', '')}。"
            )
        elif data.get("error"):
            summary = f"物流查询失败：{data.get('hint') or data['error']}"
        else:
            summary = json.dumps(data, ensure_ascii=False)
    return artifact, summary
