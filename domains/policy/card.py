"""政策域 AgentCard（skill 17-18；x-横切域）。"""

from shared.a2a_langgraph import build_agent_card


def build_card(url: str):  # noqa: ANN201
    return build_agent_card(
        name="政策知识 Agent",
        description="政策条款检索与现行版本查询（生效期硬过滤，AgentDock 政策横切域）",
        url=url,
        skills=[
            {
                "id": "policy.answer",
                "name": "政策问答",
                "description": "按自然语言问题检索生效政策条款（引用带版本与生效期）",
                "tags": ["policy"],
            },
            {
                "id": "policy.clause",
                "name": "条款查询",
                "description": "按条款号取当前生效版本（过期版本不返回）",
                "tags": ["policy"],
            },
            {
                "id": "knowledge.upsert",
                "name": "知识条目维护",
                "description": "新增/更新内部知识条目（版本化，只写 knowledge_entries）",
                "tags": ["policy"],
            },
        ],
    )
