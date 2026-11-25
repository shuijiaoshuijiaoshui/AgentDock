"""政策域工具（skill 17-18 + 2B 知识库）。

- policy.answer：权威条款（policy_docs）优先 + 补充知识（knowledge_entries）；
  冲突（knowledge.conflicts_with 命中权威条款）→ policy_override=true 并审计
- policy.clause：按条款号取现行版本
- knowledge.upsert：只写 knowledge_entries（版本化；写逻辑单份在
  publishing.upsert_knowledge_entry，与 /internal/knowledge API 共用）
"""

import json

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from domains.policy.publishing import PolicyError, upsert_knowledge_entry
from domains.policy.retriever import (
    get_effective_clause,
    search_clauses,
    search_knowledge,
)
from shared.audit import audit_from_config
from shared.auth import ensure_scope
from shared.tenant import actor_id, tenant_id


@tool
def answer_policy(query: str, config: RunnableConfig) -> str:
    """按问题检索生效政策条款（skill: policy.answer）。query 为自然语言或关键词。"""
    if not ensure_scope(config, "policy.read"):
        return json.dumps({"error": "rejected", "need_scope": "policy.read"},
                          ensure_ascii=False)
    tenant = tenant_id(config)
    clauses = search_clauses(query, tenant_id=tenant)
    clause_ids = {c.get("clause_id") for c in clauses}
    knowledge = search_knowledge(query, tenant_id=tenant)
    override_entries = [
        k for k in knowledge if k.get("conflicts_with") in clause_ids
    ]
    if override_entries:
        audit_from_config(
            config, "knowledge.override", target="policy.answer",
            params={"conflicts": [k.get("entry_id") for k in override_entries],
                    "clause_ids": sorted(clause_ids)},
            result_summary="知识与权威条款冲突，以条款为准（policy_override）",
        )
    found = bool(clauses)
    if not clauses and not knowledge:
        answer = (
            "未找到与问题直接对应的生效条款"
            "（如涉及具体商品，请提供 SKU/商品名称）；"
            "可回复「转人工」进一步处理。"
        )
    elif not clauses:
        answer = (
            "未找到权威条款；以下为补充知识（非权威），"
            "实际办理请回复「转人工」。"
        )
    else:
        answer = "已找到生效条款（见 clauses，引用需带版本与生效期）"
        if knowledge:
            answer += "；另含补充知识（knowledge，非权威）"
    return json.dumps(
        {
            "answer": answer,
            "found": found,
            "clauses": clauses,
            "knowledge": [
                {k: v for k, v in k_entry.items() if k != "score"}
                for k_entry in knowledge
            ],
            "policy_override": bool(override_entries),
        },
        ensure_ascii=False,
    )


@tool
def get_clause(clause_id: str, config: RunnableConfig) -> str:
    """按条款号查询现行生效版本（skill: policy.clause）。如 CLS-001。"""
    if not ensure_scope(config, "policy.read"):
        return json.dumps({"error": "rejected", "need_scope": "policy.read"},
                          ensure_ascii=False)
    clause = get_effective_clause(
        clause_id.strip().upper(), tenant_id=tenant_id(config)
    )
    if clause is None:
        return json.dumps(
            {"error": "not_found", "clause_id": clause_id,
             "hint": "无生效版本（可能已过期或编号不存在）"},
            ensure_ascii=False,
        )
    return json.dumps(clause, ensure_ascii=False)


@tool
def knowledge_upsert(
    title: str,
    content: str,
    config: RunnableConfig,
    entry_id: str = "",
    scope: str = "general",
    status: str = "draft",
    effective_from: str = "",
    effective_to: str = "",
    keywords: str = "",
    conflicts_with: str = "",
) -> str:
    """新增/更新知识条目（skill: knowledge.upsert）。只写 knowledge_entries；
    已存在 entry_id 时生成新版本。status: draft/submitted/published/archived。"""
    if not ensure_scope(config, "knowledge.write"):
        return json.dumps({"error": "rejected", "need_scope": "knowledge.write"},
                          ensure_ascii=False)
    tenant = tenant_id(config)
    try:
        result = upsert_knowledge_entry(
            tenant, actor_id(config), title=title, content=content, status=status,
            entry_id=entry_id, scope=scope, effective_from=effective_from,
            effective_to=effective_to, keywords=keywords,
            conflicts_with=conflicts_with,
        )
    except PolicyError as e:
        return json.dumps({"error": e.code, "detail": e.detail}, ensure_ascii=False)
    audit_from_config(
        config, "knowledge.upsert",
        target=f"knowledge:{result['entry_id']}@{result['version']}",
        params={"status": status, "conflicts_with": conflicts_with},
        result_summary=title[:100],
    )
    return json.dumps(result, ensure_ascii=False)


TOOLS = [answer_policy, get_clause, knowledge_upsert]
TOOL_TO_SKILL = {
    "answer_policy": "policy.answer",
    "get_clause": "policy.clause",
    "knowledge_upsert": "knowledge.upsert",
}
