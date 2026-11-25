"""营销经营分析域图：紧凑 ReAct 循环（同 customer 模板）。

fake 决策：保存方案 → plan_save；投流/ROI/转化 → ad_report；
选品 → product_selection；竞品/价格 → competitor_watch；
爆款/卖得 → hot_rank。
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

from domains.marketing.tools import TOOL_TO_SKILL, TOOLS


class DomainState(TypedDict):
    messages: Annotated[list, add_messages]
    artifact: dict
    summary: str


def _fake_decide(messages: list) -> AIMessage:
    last = messages[-1]
    if isinstance(last, ToolMessage):
        return AIMessage(content="")
    text = " ".join(m.content for m in messages if isinstance(m.content, str))
    if any(w in text for w in ("保存方案", "生成计划单", "落方案", "记录方案")):
        return AIMessage(content="", tool_calls=[{
            "name": "plan_save",
            "args": {"kind": "ad_adjust", "target": "CP-01",
                     "note": "按最近一次投放分析结论保存"},
            "id": "call_1"}])
    if any(w in text for w in ("投流", "投放", "ROI", "roi", "转化率", "广告")):
        return AIMessage(content="", tool_calls=[{
            "name": "ad_report", "args": {"query": text}, "id": "call_1"}])
    if any(w in text for w in ("选品",)):
        return AIMessage(content="", tool_calls=[{
            "name": "product_selection", "args": {"query": text}, "id": "call_1"}])
    if any(w in text for w in ("竞品", "价格监控", "降价", "比价")):
        return AIMessage(content="", tool_calls=[{
            "name": "competitor_watch", "args": {"query": text}, "id": "call_1"}])
    if any(w in text for w in ("爆款", "卖得", "热卖", "什么好卖", "啥好卖")):
        return AIMessage(content="", tool_calls=[{
            "name": "hot_rank", "args": {"query": text}, "id": "call_1"}])
    # 兜底：营销域默认给投放效果（nomatch 由编排层处理）
    return AIMessage(content="", tool_calls=[{
        "name": "ad_report", "args": {"query": text}, "id": "call_1"}])


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
        "authority_domain": "marketing",
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
    """营销域人话摘要：分析结论 + 确定性方案（fake 模式下"方案"即从此生成）。"""
    artifact = state.get("artifact") or {}
    summary = state.get("summary") or ""
    if summary:
        return artifact, summary
    data = artifact.get("data")
    skill = str(artifact.get("skill") or "")
    if isinstance(data, dict) and data.get("error"):
        return artifact, f"查询失败：{data.get('detail') or data['error']}"
    if skill == "ad.report" and isinstance(data, dict):
        total = data.get("total") or {}
        plans = data.get("plan") or []
        plan_text = "；".join(
            f"{p.get('campaign_id')} → {p.get('action')}" for p in plans[:3])
        summary = (
            f"近 {data.get('days', 14)} 天投放：总花费 {total.get('spend', 0)} 元，"
            f"GMV {total.get('gmv', 0)} 元，整体 ROI {total.get('roi', 0)}。"
            f"投放方案：{plan_text or '暂无在投计划'}。"
            "方案可回复「保存方案」存为计划单，实际执行需人工审批。"
        )
    elif skill == "selection.assist" and isinstance(data, dict):
        cands = data.get("candidates") or []
        if cands:
            items = "；".join(
                f"{c.get('sku')} {c.get('name', '')}（{c.get('reason', '')}）"
                for c in cands[:3])
            summary = f"选品候选 top {len(cands[:3])}：{items}。"
        else:
            summary = "暂无满足条件的选品候选。"
    elif skill == "competitor.watch" and isinstance(data, dict):
        alerts = data.get("alerts") or []
        watched = data.get("watchlist") or []
        if alerts:
            alert_text = "；".join(a.get("alert", "") for a in alerts[:3])
            summary = (
                f"监控 {len(watched)} 个竞品，{len(alerts)} 个降价提醒：{alert_text}。"
            )
        else:
            summary = f"监控 {len(watched)} 个竞品，近 7 天无显著降价（≥5%）。"
    elif skill == "hot.rank" and isinstance(data, dict):
        top = data.get("top") or []
        if top:
            items = "；".join(
                f"{t.get('sku')} {t.get('name', '')}（销量 {t.get('qty_now', 0)}，"
                f"环比 {t.get('growth', 0):+.0%}）" for t in top)
            summary = (
                f"近 {data.get('days', 7)} 天爆款 top{len(top)}：{items}。"
                "备货结论请结合履约域可承诺库存（hot_restock 已联动）。"
            )
        else:
            summary = f"近 {data.get('days', 7)} 天暂无明显爆款（环比数据不足）。"
    elif skill == "plan.save" and isinstance(data, dict):
        if data.get("status") == "pending_confirm":
            summary = (
                f"方案已生成待确认（{data.get('kind')} → {data.get('target')}），"
                "请通过确认卡确认后存为计划单。"
            )
        else:
            summary = (
                f"计划单 {data.get('plan_id', '')} 已存档"
                f"（{data.get('kind')} → {data.get('target')}，状态 "
                f"{data.get('status')}）。实际执行需人工审批。"
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
    g.add_node("tools", audited_tool_node(TOOLS, "marketing", TOOL_TO_SKILL))
    g.add_node("emit_artifact", emit_artifact)
    g.add_edge(START, "agent")
    g.add_conditional_edges("agent", _route_after_agent,
                            {"tools": "tools", "emit_artifact": "emit_artifact"})
    g.add_edge("tools", "agent")
    g.add_edge("emit_artifact", END)
    return g.compile(checkpointer=checkpointer)
