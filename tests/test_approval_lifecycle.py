"""审批单生命周期：同目标去重 + 执行联动 supersede（M4 补充）。

- find_pending：同 action/target 的未过期待审批单查询（refund_settle 创建前去重用）
- consume：执行时联动把同目标其余 pending/approved 审批单置为 superseded
  ——同一业务对象只允许一张生效审批单，杜绝悬空待办。
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from shared.approvals import (  # noqa: E402
    approve,
    consume,
    create_approval,
    find_pending,
    get_approval,
)

TENANT = "t_demo"
TARGET = "SO-9105"  # 用独立目标，避免与其他用例的审批单互相干扰


def _create(target: str = TARGET, requested_by: str = "U1") -> dict:
    return create_approval(
        tenant_id=TENANT, action="refund", target=target, amount=100.0,
        requested_by=requested_by, requested_role="customer",
        required_approvals=1, approver_roles=("supervisor", "finance", "admin"),
        dual_control=False, reason="测试", context_id="cx-lifecycle",
        task_id="t-lifecycle", conversation_id="cx-lifecycle", resume={},
    )


def test_find_pending_hits_same_target_only():
    a = _create()
    hit = find_pending(TENANT, action="refund", target=TARGET)
    assert hit is not None and hit["id"] == a["id"]
    assert find_pending(TENANT, action="refund", target="SO-不存在") is None
    # 其他 action 不命中
    assert find_pending(TENANT, action="invoice", target=TARGET) is None


def test_consume_supersedes_sibling_approvals():
    a1 = _create(requested_by="U1")
    a2 = _create(requested_by="U2")  # 同目标的第二张（历史并发残留场景）
    assert a1["id"] != a2["id"]

    approve(TENANT, a1["id"], actor_id="FIN-001", role="finance")
    consumed = consume(
        TENANT, a1["id"], action="refund", target=TARGET, amount=100.0,
        actor_id="FIN-001", role="finance",
    )
    assert consumed.get("ok") is True, consumed

    assert get_approval(TENANT, a1["id"])["status"] == "executed"
    sibling = get_approval(TENANT, a2["id"])
    assert sibling["status"] == "superseded"


def test_consume_does_not_touch_other_targets():
    a1 = _create(target=TARGET)
    other = _create(target="SO-9106")
    approve(TENANT, a1["id"], actor_id="FIN-001", role="finance")
    consume(
        TENANT, a1["id"], action="refund", target=TARGET, amount=100.0,
        actor_id="FIN-001", role="finance",
    )
    assert get_approval(TENANT, other["id"])["status"] == "pending"


def test_superseded_is_terminal():
    """superseded 是终态：不能再被 approve / consume。"""
    a1 = _create(target=TARGET)
    a2 = _create(target=TARGET)
    approve(TENANT, a1["id"], actor_id="FIN-001", role="finance")
    consume(
        TENANT, a1["id"], action="refund", target=TARGET, amount=100.0,
        actor_id="FIN-001", role="finance",
    )
    # superseded 是终态：approve/consume 返回结构化错误，不再进入流程
    assert approve(TENANT, a2["id"], actor_id="FIN-002",
                   role="finance").get("error") == "status_superseded"
    assert consume(
        TENANT, a2["id"], action="refund", target=TARGET, amount=100.0,
        actor_id="FIN-001", role="finance",
    ).get("error") == "status_superseded"
