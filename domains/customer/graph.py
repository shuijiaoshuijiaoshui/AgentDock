"""客户域图：紧凑 ReAct 循环（同 fulfillment 模板）。

fake 决策：额度/授信/账期 → credit_query；对账 → reconciliation_query；
投诉 → complaint_list；客户360 → customer_overview；
坐席检索（查询客户 X 的信息）→ customer_search；其余 → customer_profile。
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

from domains.customer.tools import TOOL_TO_SKILL, TOOLS


class DomainState(TypedDict):
    messages: Annotated[list, add_messages]
    artifact: dict
    summary: str


_PERIOD_RE = re.compile(r"\d{4}-\d{2}")
_SEARCH_RE = re.compile(r"查询客户[:：]?\s*(.+?)\s*(?:的信息)?(?:。|$)")


def _fake_decide(messages: list) -> AIMessage:
    last = messages[-1]
    if isinstance(last, ToolMessage):
        return AIMessage(content="")
    text = " ".join(m.content for m in messages if isinstance(m.content, str))
    if "客户360" in text or "客户 360" in text:
        return AIMessage(content="", tool_calls=[{
            "name": "customer_overview", "args": {}, "id": "call_1"}])
    m = _SEARCH_RE.search(text)
    if m and m.group(1).strip():
        return AIMessage(content="", tool_calls=[{
            "name": "customer_search",
            "args": {"query": m.group(1).strip()}, "id": "call_1"}])
    if any(w in text for w in ("额度", "授信", "账期", "信用")):
        return AIMessage(content="", tool_calls=[{
            "name": "credit_query", "args": {}, "id": "call_1"}])
    if any(w in text for w in ("对账",)):
        pm = _PERIOD_RE.search(text)
        return AIMessage(content="", tool_calls=[{
            "name": "reconciliation_query",
            "args": {"period": pm.group(0) if pm else ""}, "id": "call_1"}])
    if any(w in text for w in ("投诉",)):
        return AIMessage(content="", tool_calls=[{
            "name": "complaint_list", "args": {}, "id": "call_1"}])
    return AIMessage(content="", tool_calls=[{
        "name": "customer_profile", "args": {}, "id": "call_1"}])


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
        "authority_domain": "customer",
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


def artifact_builder(state: dict) -> tuple[dict, str]:
    """客户域人话摘要：客户可见回复不暴露内部 JSON 字段。"""
    artifact = state.get("artifact") or {}
    summary = state.get("summary") or ""
    if summary:
        return artifact, summary
    data = artifact.get("data")
    skill = str(artifact.get("skill") or "")
    if isinstance(data, dict) and data.get("error"):
        return artifact, f"查询失败：{data.get('hint') or data['error']}"
    if skill == "customer.profile" and isinstance(data, dict):
        summary = (
            f"客户 {data.get('name', '')}（{data.get('customer_id', '')}），"
            f"等级 {data.get('dealer_level') or data.get('tier') or '-'}，"
            f"折扣 {data.get('discount', '-')}，"
            f"客户经理 {data.get('account_manager') or '-'}。"
        )
    elif skill == "credit.query" and isinstance(data, dict):
        summary = (
            f"授信额度 {data.get('credit_limit', 0)} 元，已用 {data.get('credit_used', 0)} 元，"
            f"剩余 {data.get('credit_remaining', 0)} 元；"
            f"账期 {data.get('credit_days', '-')} 天，到期日 {data.get('credit_due_date') or '-'}。"
        )
    elif skill == "reconciliation.query" and isinstance(data, list):
        if data:
            first = data[0]
            summary = (
                f"共查到 {len(data)} 期对账单；最近一期 {first.get('period', '')}，"
                f"金额 {first.get('amount', '')} 元，状态 {first.get('status', '')}，"
                f"开票状态 {first.get('invoice_status', '')}。"
            )
        else:
            summary = "暂无对账单。"
    elif skill == "complaint.list" and isinstance(data, dict):
        summary = str(data.get("answer") or "已查询投诉记录。")
    elif skill == "customer.search" and isinstance(data, list):
        if data:
            items = "、".join(
                f"{r.get('customer_id', '')}（{r.get('name', '')}）" for r in data[:5])
            more = f" 等 {len(data)} 位" if len(data) > 5 else ""
            summary = f"查到{len(data)}位客户{more}：{items}。"
        else:
            summary = "未找到匹配的客户（请确认 ID/手机号/姓名）。"
    elif skill == "customer.overview" and isinstance(data, dict):
        cust = data.get("customer") or {}
        orders = data.get("orders") or []
        tickets = data.get("tickets") or []
        comps = data.get("complaints") or []
        reconc = data.get("reconciliations") or []
        summary = (
            f"客户 {cust.get('name', '')}（{cust.get('customer_id', '')}）360 视图："
            f"近期订单 {len(orders)} 笔"
            + (f"（最近 {orders[0].get('order_no', '')} {orders[0].get('status', '')}）"
               if orders else "")
            + f"，工单 {len(tickets)} 个，投诉 {len(comps)} 条"
            + (f"，最近对账 {reconc[0].get('period', '')} {reconc[0].get('status', '')}"
               if reconc else "")
            + "。"
        )
    else:
        summary = json.dumps(data, ensure_ascii=False)
    return artifact, summary


def build_domain_graph(checkpointer=None):  # noqa: ANN201
    def agent_node(state: DomainState, config: RunnableConfig) -> dict:
        if get_settings().llm_provider == "fake":
            return {"messages": [_fake_decide(state["messages"])]}
        llm = get_main_llm().bind_tools(TOOLS)
        return {"messages": [llm.invoke(sanitize_messages_for_llm(state["messages"]), config=config)]}

    g = StateGraph(DomainState)
    g.add_node("agent", agent_node)
    g.add_node("tools", audited_tool_node(TOOLS, "customer", TOOL_TO_SKILL))
    g.add_node("emit_artifact", emit_artifact)
    g.add_edge(START, "agent")
    g.add_conditional_edges("agent", _route_after_agent,
                            {"tools": "tools", "emit_artifact": "emit_artifact"})
    g.add_edge("tools", "agent")
    g.add_edge("emit_artifact", END)
    return g.compile(checkpointer=checkpointer)
