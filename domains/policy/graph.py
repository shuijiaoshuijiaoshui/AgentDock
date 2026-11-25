"""政策域图：紧凑 ReAct 循环（同 merchandising 模板）。

llm(bind tools) → tools → … → emit_artifact。fake 模式规则选工具。
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

from domains.policy.tools import TOOL_TO_SKILL, TOOLS


class DomainState(TypedDict):
    messages: Annotated[list, add_messages]
    artifact: dict
    summary: str


_CLAUSE_RE = re.compile(r"CLS-\d+", re.IGNORECASE)


def _fake_decide(messages: list) -> AIMessage:
    last = messages[-1]
    if isinstance(last, ToolMessage):
        return AIMessage(content="")  # 收口 → emit_artifact 按数据生成摘要
    text = " ".join(m.content for m in messages if isinstance(m.content, str))
    m = _CLAUSE_RE.search(text)
    if m:
        return AIMessage(
            content="",
            tool_calls=[{"name": "get_clause", "args": {"clause_id": m.group(0).upper()}, "id": "call_1"}],
        )
    q = re.sub(r"是什么|什么|怎么|如何|[的吗呢请问一下查询看看帮]", "", text).strip() or "退货政策"
    return AIMessage(
        content="",
        tool_calls=[{"name": "answer_policy", "args": {"query": q}, "id": "call_1"}],
    )


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
        "authority_domain": "policy",
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
    g.add_node("tools", audited_tool_node(TOOLS, "policy", TOOL_TO_SKILL))
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
    if not summary:
        clauses = data.get("clauses") if isinstance(data, dict) else None
        if clauses:
            first = clauses[0]
            summary = (
                f"{first['title']}（{first['clause_id']} {first['version']}，"
                f"生效 {first['effective_range']}）：{first['text']}"
            )
            if len(clauses) > 1:
                summary += f" 等共 {len(clauses)} 条生效条款。"
        elif isinstance(data, dict) and data.get("clause_id"):
            summary = (
                f"{data['title']}（{data['clause_id']} {data['version']}，"
                f"生效 {data['effective_range']}）：{data['text']}"
            )
        elif isinstance(data, dict) and data.get("error"):
            summary = f"条款查询失败：{data.get('hint') or data['error']}"
        elif isinstance(data, dict) and data.get("knowledge") and not clauses:
            first = data["knowledge"][0]
            summary = (
                f"未找到权威条款；补充知识（非权威）：{first.get('title', '')}——"
                f"{first.get('content', '')}"
                "（实际办理请回复「转人工」）"
            )
        elif isinstance(data, dict) and data.get("found") is False:
            summary = (
                "未找到与问题直接对应的生效条款"
                "（如涉及具体商品，请提供 SKU/商品名称）；"
                "可回复「转人工」进一步处理。"
            )
        elif isinstance(data, dict) and data.get("answer") == "未找到生效条款":
            summary = "未找到生效条款。"
        else:
            summary = json.dumps(data, ensure_ascii=False)
    return artifact, summary
