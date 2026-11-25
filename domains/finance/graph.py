"""财务域图：紧凑 ReAct 循环（同 merchandising 模板）。

fake 决策：退款 → settle/status；开票 → invoice；默认 → 账单查询。
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

from domains.finance.tools import TOOL_TO_SKILL, TOOLS


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
    no = om.group(0).upper() if om else ""
    if "退款" in text and any(w in text for w in ("执行", "发起", "办理", "退钱", "并")):
        return AIMessage(
            content="", tool_calls=[{
                "name": "refund_settle", "args": {"order_no": no}, "id": "call_1"}])
    if "退款" in text:
        return AIMessage(
            content="", tool_calls=[{
                "name": "refund_status_query", "args": {"order_no": no}, "id": "call_1"}])
    if ("发票" in text or "开票" in text) and any(
        w in text for w in ("进度", "状态", "记录", "到哪", "查询", "查一下")
    ):
        return AIMessage(
            content="", tool_calls=[{
                "name": "invoice_status_query", "args": {"order_no": no},
                "id": "call_1"}])
    if "发票" in text or "开票" in text:
        tm = re.search(r"抬头[：:]\s*([^\s，。]+)", text)
        return AIMessage(
            content="", tool_calls=[{
                "name": "invoice_issue",
                "args": {"order_no": no, "title": tm.group(1) if tm else "默认抬头"},
                "id": "call_1"}])
    return AIMessage(
        content="", tool_calls=[{
            "name": "billing_statement", "args": {"order_no": no}, "id": "call_1"}])


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
        "authority_domain": "finance",
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
    g.add_node("tools", audited_tool_node(TOOLS, "finance", TOOL_TO_SKILL))
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
    data = artifact.get("data")
    if summary:
        return artifact, summary
    if isinstance(data, list):
        if not data:
            summary = "暂无记录。"
        elif "order_no" in data[0] and "status" in data[0]:
            summary = (
                f"共查到 {len(data)} 条记录；最近一条订单 {data[0].get('order_no', '')}，"
                f"状态 {data[0].get('status', '')}。"
            )
        else:
            summary = f"共查到 {len(data)} 条记录。"
        return artifact, summary
    if not isinstance(data, dict):
        return artifact, json.dumps(data, ensure_ascii=False)
    if data.get("error"):
        summary = f"财务查询失败：{data.get('hint') or data['error']}"
    elif data.get("status") == "settled":
        summary = (
            f"订单 {data['order_no']} 退款已执行：{data['amount']} 元"
            + ("（幂等重放，未二次退款）" if data.get("idempotent") else "。")
        )
    elif data.get("status") == "pending_confirm":
        p = data.get("preview") or {}
        summary = (
            f"将为订单 {p.get('order_no')} 开具发票（抬头：{p.get('title')}，"
            f"金额 {p.get('amount')} 元）。确认后才会执行。"
        )
    elif data.get("status") == "issued":
        summary = f"发票已开具：{data['title']}，{data['amount']} 元（{data['order_no']}）。"
    elif "invoices" in data:
        invoices = data.get("invoices") or []
        if not invoices:
            summary = "暂无开票记录。"
        else:
            first = invoices[0]
            summary = (
                f"共 {len(invoices)} 张开票记录；最近一张订单 {first.get('order_no', '')}，"
                f"抬头 {first.get('title', '')}，状态 {first.get('status', '')}。"
            )
    elif "amount" in data:
        summary = (
            f"订单 {data['order_no']} 实付 {data['amount']} 元"
            f"（{data.get('pay_channel', '')}，支付于 {data.get('paid_at', '')}）。"
        )
    else:
        summary = json.dumps(data, ensure_ascii=False)
    return artifact, summary
