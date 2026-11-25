"""编排器共享 State（技术方案 2.1，M1 单域 dispatch 子集）。

修正技术方案示意的 reducer bug：`decision_log` / `cancelled_domains` /
`paused_questions` 是 list 类型，不能用 `merge_dict`（{**left, **right} 对 list
会 TypeError），必须用 `merge_list`；`artifacts` 是 dict，用 `merge_dict`。
并行分支（M3 的 Send 扇出）各自只写自己的 key，reducer 保证合并安全。
"""

from typing import Annotated, Literal, TypedDict

from langgraph.graph.message import add_messages


def merge_dict(left: dict, right: dict) -> dict:
    return {**left, **right}


def merge_list(left: list, right: list) -> list:
    return left + right


class Plan(TypedDict):
    intent: str
    domains: list[str]
    mode: Literal["single", "parallel", "serial", "arbitrate", "saga"]
    deps: dict  # 域依赖（arbitrate DAG）：{domain: [upstream...]}；serial 隐式为链序


class OrchestratorState(TypedDict):
    messages: Annotated[list, add_messages]
    user_text: str
    trace_id: str
    snapshot_id: str
    context_id: str            # A2A 会话标识（多轮同 contextId，转人工按它关联历史）
    customer_id: str           # 客户身份（customer 角色取 token.sub；坐席按会话对象）
    tenant_id: str             # 系统槽位：来自 JWT claim，禁止从自然语言获取
    actor_id: str
    actor_role: str
    scopes: list[str]
    pii_masked: int            # 输入侧命中的 PII 数量（只记数量，不落原文）

    intent: str
    slots: dict
    confidence: float
    plan: Plan | None
    # 1B：渠道层结构化 action（确认/审批续跑）绕过 NLU 的确定性入口
    forced_intent: str
    forced_slots: dict
    confirm_token: str
    approval_id: str

    # 人机协同（PRD 7.12）：碰壁计数（由渠道层经 metadata 传入、跨 Task 续计）
    hit_wall_count: int
    risk_flag: bool
    handoff_done: bool

    # 多轮历史（M4 / PRD 10.4）：渠道层随 metadata 续传——滚动摘要 + 最近 N 轮
    # 原文 + 掉窗轮次；本 Task 内只读，摘要生成结果经 artifact 回写渠道层
    history: dict
    session_summary: dict

    # 并行安全（M3 起多分支并发写）：dict 合并 / list 拼接
    artifacts: Annotated[dict[str, dict], merge_dict]
    decision_log: Annotated[list[dict], merge_list]
    cancelled_domains: Annotated[list[str], merge_list]
    paused_questions: Annotated[list[dict], merge_list]

    clarify_count: int
    # 域侧 input-required 的续聊定位（M1 单域：最多一个挂起任务）
    pending_task: dict | None
    # 澄清/域追问的回填内容（ask_user → dispatch resume 用）
    resume_reply: str
    final_text: str
    prompt_version: str
    model: str

    # 混沌注入（M3 Saga 演练）：渠道层经 metadata 传入，如 finance_settle_fail
    chaos: str
    # Saga 补偿结果（saga_compensate 节点写入，synthesize 拼装口径）
    saga_result: dict
