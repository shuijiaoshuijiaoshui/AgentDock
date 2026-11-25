"""商品与营销域图：紧凑 ReAct 循环（技术方案 3.2）。

llm(bind tools) → tools → … → emit_artifact。
- 真实模式：GLM 主模型做域内多步推理。
- fake 模式（LLM_PROVIDER=fake）：规则路由到工具，跑通链路不调真模型。
- 架构级约束（PRD 7.3.7）：域图里没有“调用其他域 Agent”的工具，禁止横向委托。
"""

import json
import re
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any, Literal, TypedDict

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

from domains.merchandising.tools import TOOL_TO_SKILL, TOOLS


class DomainState(TypedDict):
    messages: Annotated[list, add_messages]
    artifact: dict
    summary: str


_SKU_RE = re.compile(r"SKU[-A-Za-z0-9]*", re.IGNORECASE)


def _fake_decide(messages: list) -> AIMessage:
    """fake 模式的规则决策：按关键词/正则选工具，工具结果回来后收口。"""
    last = messages[-1]
    if isinstance(last, ToolMessage):
        # 收口留空摘要 → emit_artifact/artifact_builder 按数据生成人话摘要
        return AIMessage(content="")
    text = " ".join(m.content for m in messages if isinstance(m.content, str))
    skus = [m.group(0).upper() for m in _SKU_RE.finditer(text)]
    if "对比" in text or "比较" in text:
        return AIMessage(
            content="",
            tool_calls=[
                {"name": "compare_matrix", "args": {"skus": ",".join(skus)}, "id": "call_1"}
            ],
        )
    if any(w in text for w in ("国补", "补贴", "以旧换新")):
        sku = skus[0] if skus else ""
        return AIMessage(
            content="",
            tool_calls=[
                {"name": "subsidy_check", "args": {"sku": sku}, "id": "call_1"}
            ],
        )
    if any(w in text for w in ("优惠", "到手", "多少钱", "价格")):
        sku = skus[0] if skus else ""
        return AIMessage(
            content="",
            tool_calls=[
                {"name": "promo_evaluate", "args": {"sku": sku}, "id": "call_1"}
            ],
        )
    if skus:
        return AIMessage(
            content="",
            tool_calls=[{"name": "product_lookup", "args": {"sku": skus[0]}, "id": "call_1"}],
        )
    keywords = re.sub(r"[查询看看帮我一下的商品详情]", "", text).strip() or "商品"
    return AIMessage(
        content="",
        tool_calls=[{"name": "product_retrieve", "args": {"keywords": keywords}, "id": "call_1"}],
    )


def _route_after_agent(state: DomainState) -> Literal["tools", "emit_artifact"]:
    last = state["messages"][-1]
    return "tools" if getattr(last, "tool_calls", None) else "emit_artifact"


def emit_artifact(state: DomainState, config: RunnableConfig) -> dict:
    """工具结果 → 统一 Artifact（authority/as_of/snapshot_id 必填，技术方案 3.1）。"""
    tool_msgs = [m for m in state["messages"] if isinstance(m, ToolMessage)]
    data: Any = {}
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
        "authority_domain": "merchandising",
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
    g.add_node("tools", audited_tool_node(TOOLS, "merchandising", TOOL_TO_SKILL))
    g.add_node("emit_artifact", emit_artifact)
    g.add_edge(START, "agent")
    g.add_conditional_edges("agent", _route_after_agent, {"tools": "tools", "emit_artifact": "emit_artifact"})
    g.add_edge("tools", "agent")
    g.add_edge("emit_artifact", END)
    return g.compile(checkpointer=checkpointer)


def artifact_builder(state: dict) -> tuple[dict, str]:
    """executor 取产物：summary 为空时按数据生成一句话摘要。"""
    artifact = state.get("artifact") or {}
    summary = state.get("summary") or ""
    data = artifact.get("data") or {}
    if not summary:
        if "matrix" in data:
            names = "、".join(r.get("name", "") for r in data["matrix"])
            summary = f"对比矩阵已生成：{names}。"
        elif "estimate_price" in data:
            summary = (
                f"{data.get('sku')} 估算到手价 {data.get('estimate_price')} 元"
                f"（{'；'.join(data.get('applied', []))}，估算值）。"
            )
        elif "eligible" in data:
            if data.get("eligible"):
                summary = (
                    f"{data.get('sku')} 符合{data.get('title', '国补')}："
                    f"原价 {data.get('list_price')} 元，补贴 {data.get('subsidy_amount')} 元，"
                    f"预计到手 {data.get('final_price')} 元"
                    f"（每人限 {data.get('per_user_limit')} 件，估算值）。"
                )
            else:
                summary = f"{data.get('sku')} 暂不符合国补：{data.get('reason')}。"
        elif "name" in data:
            stock = data.get("stock")
            stock_txt = "有货" if (stock or 0) > 0 else "无货"
            summary = (
                f"{data.get('sku')}「{data.get('name')}」：价格 {data.get('price')} 元，"
                f"库存 {stock}（{stock_txt}），状态 {data.get('status')}。"
            )
        elif isinstance(data, list):
            summary = f"共找到 {len(data)} 个候选商品。"
        else:
            summary = json.dumps(data, ensure_ascii=False)
    return artifact, summary
