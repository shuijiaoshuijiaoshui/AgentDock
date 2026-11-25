"""Phase 4 工作台 API 合同测试：队列/摘要/备注/驳回/转交/快捷回复/知识发布权限。

复用 test_customer_e2e 的 stack（orchestrator + console + customer 域 + securityd）。
"""

import httpx
import pytest

from tests.test_customer_e2e import stack  # noqa: F401  (module-scope fixture)

pytestmark = pytest.mark.usefixtures("stack")


def _console(stack) -> str:  # noqa: ANN001
    return f"http://127.0.0.1:{stack['ports']['console']}"


def _login(stack, username: str, surface: str = "workbench", tenant: str = "t_demo") -> dict:
    r = httpx.post(
        _console(stack) + "/api/auth/login",
        json={"tenant_id": tenant, "username": username,
              "password": "demo123", "surface": surface},
        timeout=10, trust_env=False,
    )
    assert r.status_code == 200, r.text
    data = r.json()
    return {"Authorization": f"Bearer {data['token']}"}


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _send_customer(stack, text: str):  # noqa: ANN001
    headers = _login(stack, "customer", surface="customer")
    r = httpx.post(
        _console(stack) + "/api/customer/send", headers=headers,
        json={"text": text}, timeout=60, trust_env=False,
    )
    assert r.status_code == 200, r.text
    return r.json(), headers


def test_queues_tabs_filters_and_read(stack):  # noqa: ANN001
    agent = _login(stack, "agent")
    r = httpx.get(
        _console(stack) + "/api/workbench/queues?tab=pending&sort=waiting",
        headers=agent, timeout=15, trust_env=False,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["tab"] == "pending"
    assert set(body["counts"]) == {"pending", "review", "human", "closed"}
    assert body["sla_seconds"] > 0
    for item in body["items"]:
        assert item["state"] == "PENDING_HANDOFF"
        assert "priority" in item and "unread" in item
        assert "waiting_seconds" in item and "sla_breached" in item
    # bad tab / priority → 400
    for query in ("tab=evil", "priority=urgent", "sort=evil", "cursor=-1", "limit=0"):
        bad = httpx.get(
            _console(stack) + f"/api/workbench/queues?{query}",
            headers=agent, timeout=15, trust_env=False,
        )
        assert bad.status_code == 400, (query, bad.status_code)
    # customer role → 403
    customer = _login(stack, "customer", surface="customer")
    denied = httpx.get(
        _console(stack) + "/api/workbench/queues", headers=customer,
        timeout=15, trust_env=False,
    )
    assert denied.status_code == 403


def test_summary_and_cache(stack):  # noqa: ANN001
    result, _ = _send_customer(stack, "查一下我的对账单")
    cid = result["conversation_id"]
    agent = _login(stack, "agent")
    first = httpx.get(
        _console(stack) + f"/api/conversations/{cid}/summary", headers=agent,
        timeout=30, trust_env=False,
    )
    assert first.status_code == 200
    s = first.json()["summary"]
    for key in ("summary_text", "customer", "request", "suggested_actions", "stats"):
        assert key in s, key
    assert s["customer"]["customer_id"] == "CU-1001"
    assert first.json()["cached"] is False
    second = httpx.get(
        _console(stack) + f"/api/conversations/{cid}/summary", headers=agent,
        timeout=30, trust_env=False,
    )
    assert second.json()["cached"] is True
    refreshed = httpx.get(
        _console(stack) + f"/api/conversations/{cid}/summary?refresh=1",
        headers=agent, timeout=30, trust_env=False,
    )
    assert refreshed.json()["cached"] is False
    # customer 不可见
    customer = _login(stack, "customer", surface="customer")
    denied = httpx.get(
        _console(stack) + f"/api/conversations/{cid}/summary", headers=customer,
        timeout=15, trust_env=False,
    )
    assert denied.status_code == 403


def test_notes_lifecycle_and_isolation(stack):  # noqa: ANN001
    result, _ = _send_customer(stack, "你好")
    cid = result["conversation_id"]
    agent = _login(stack, "agent")
    created = httpx.post(
        _console(stack) + f"/api/conversations/{cid}/notes", headers=agent,
        json={"content": "内部备注：客户要求今天回复"}, timeout=15, trust_env=False,
    )
    assert created.status_code == 201
    assert created.json()["author_id"] == "AG-001"
    listed = httpx.get(
        _console(stack) + f"/api/conversations/{cid}/notes", headers=agent,
        timeout=15, trust_env=False,
    )
    notes = listed.json()["notes"]
    assert any(n["content"].startswith("内部备注") for n in notes)
    # 客户不可读写备注
    customer = _login(stack, "customer", surface="customer")
    assert httpx.get(
        _console(stack) + f"/api/conversations/{cid}/notes", headers=customer,
        timeout=15, trust_env=False,
    ).status_code == 403
    # 消息流不包含备注内容（客户侧隔离）
    msgs = httpx.get(
        _console(stack) + f"/api/conversations/{cid}/messages", headers=customer,
        timeout=15, trust_env=False,
    ).json()
    assert all("内部备注" not in (m.get("content") or "") for m in msgs)


def test_reject_answer_flow(stack):  # noqa: ANN001
    result, customer = _send_customer(stack, "查询订单 SO-2001 的状态")
    cid = result["conversation_id"]
    httpx.post(
        _console(stack) + "/api/customer/send", headers=customer,
        json={"conversation_id": cid, "text": "转人工"}, timeout=60, trust_env=False,
    )
    msgs = httpx.get(
        _console(stack) + f"/api/conversations/{cid}/messages", headers=customer,
        timeout=15, trust_env=False,
    ).json()
    ai_msg = next(m for m in msgs if m["role"] == "ai")
    agent = _login(stack, "agent")
    # 未接管（非 human 模式）→ 409
    early = httpx.post(
        _console(stack) + f"/api/conversations/{cid}/reject-answer", headers=agent,
        json={"message_id": ai_msg["id"], "edited_answer": "改写", "reason": "口径"},
        timeout=15, trust_env=False,
    )
    assert early.status_code in (409, 200)  # 非人工模式 409；已人工则直接成功
    httpx.post(
        _console(stack) + f"/api/conversations/{cid}/takeover", headers=agent,
        timeout=30, trust_env=False,
    )
    ok = httpx.post(
        _console(stack) + f"/api/conversations/{cid}/reject-answer", headers=agent,
        json={"message_id": ai_msg["id"], "edited_answer": "改写后的答复",
              "reason": "金额口径不准"},
        timeout=30, trust_env=False,
    )
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["feedback"]["reason"] == "金额口径不准"
    assert body["message"]["role"] == "agent"
    # 非 AI 消息 → 400；缺参 → 400
    agent_msg = body["message"]
    bad = httpx.post(
        _console(stack) + f"/api/conversations/{cid}/reject-answer", headers=agent,
        json={"message_id": agent_msg["id"], "edited_answer": "x", "reason": "y"},
        timeout=15, trust_env=False,
    )
    assert bad.status_code == 400
    missing = httpx.post(
        _console(stack) + f"/api/conversations/{cid}/reject-answer", headers=agent,
        json={"message_id": ai_msg["id"]},
        timeout=15, trust_env=False,
    )
    assert missing.status_code == 400


def test_agents_and_transfer(stack):  # noqa: ANN001
    result, _ = _send_customer(stack, "转人工")
    cid = result["conversation_id"]
    agent = _login(stack, "agent")
    listing = httpx.get(
        _console(stack) + "/api/agents", headers=agent, timeout=15, trust_env=False,
    )
    assert listing.status_code == 200
    agents = listing.json()["agents"]
    assert all("load" in a for a in agents)
    assert any(a["user_id"] == "SUP-001" for a in agents)
    # 转交到主管：状态仍是人工链路（接管前转交即直接指派）
    to_id = next(a["user_id"] for a in agents if a["user_id"] == "SUP-001")
    transfer = httpx.post(
        _console(stack) + f"/api/conversations/{cid}/transfer", headers=agent,
        json={"to_agent_id": to_id}, timeout=30, trust_env=False,
    )
    assert transfer.status_code == 200, transfer.text
    assert transfer.json()["assigned_agent_id"] == to_id
    assert transfer.json()["state"] == "HUMAN_ACTIVE"
    # 转给不存在坐席 → 400；转给 customer 角色 → 400
    # 注意：首次转交后会话已归属 SUP-001，AG-001 再操作会被归属校验拒绝（403），
    # 因此这里用 supervisor 发起，才能验证到参数校验分支。
    supervisor = _login(stack, "supervisor")
    bad = httpx.post(
        _console(stack) + f"/api/conversations/{cid}/transfer", headers=supervisor,
        json={"to_agent_id": "NOPE"}, timeout=15, trust_env=False,
    )
    assert bad.status_code == 400
    bad_role = httpx.post(
        _console(stack) + f"/api/conversations/{cid}/transfer", headers=supervisor,
        json={"to_agent_id": "CU-1001"}, timeout=15, trust_env=False,
    )
    assert bad_role.status_code == 400
    # customer 不可转交（角色直接 403）
    customer = _login(stack, "customer", surface="customer")
    denied = httpx.post(
        _console(stack) + f"/api/conversations/{cid}/transfer", headers=customer,
        json={"to_agent_id": to_id}, timeout=15, trust_env=False,
    )
    assert denied.status_code == 403
    # 向 customer 角色转交（本租户有效用户但角色不符）→ 400
    bad_target = httpx.post(
        _console(stack) + f"/api/conversations/{cid}/transfer", headers=supervisor,
        json={"to_agent_id": "CU-1002"}, timeout=15, trust_env=False,
    )
    assert bad_target.status_code == 400
    # AG-001 对已归属 SUP-001 的会话再转交 → 403（归属校验）
    hijack = httpx.post(
        _console(stack) + f"/api/conversations/{cid}/transfer", headers=agent,
        json={"to_agent_id": "AG-001"}, timeout=15, trust_env=False,
    )
    assert hijack.status_code == 403


def test_quick_replies_per_tenant(stack):  # noqa: ANN001
    agent = _login(stack, "agent")
    r = httpx.get(
        _console(stack) + "/api/quick-replies", headers=agent,
        timeout=15, trust_env=False,
    )
    assert r.status_code == 200
    replies = r.json()["quick_replies"]
    assert replies and all("content" in q for q in replies)
    other = _login(stack, "agent2", tenant="t_other")
    r2 = httpx.get(
        _console(stack) + "/api/quick-replies", headers=other,
        timeout=15, trust_env=False,
    )
    assert r2.status_code == 200
    assert {q["reply_id"] for q in r2.json()["quick_replies"]} == \
        {q["reply_id"] for q in replies}


def test_mark_read_clears_unread(stack):  # noqa: ANN001
    result, _ = _send_customer(stack, "转人工")
    cid = result["conversation_id"]
    agent = _login(stack, "agent")
    queues = httpx.get(
        _console(stack) + "/api/workbench/queues?tab=pending", headers=agent,
        timeout=15, trust_env=False,
    ).json()
    item = next(i for i in queues["items"] if i["id"] == cid)
    assert item["unread"] > 0
    read = httpx.post(
        _console(stack) + f"/api/conversations/{cid}/read", headers=agent,
        json={}, timeout=15, trust_env=False,
    )
    assert read.status_code == 200
    queues2 = httpx.get(
        _console(stack) + "/api/workbench/queues?tab=pending", headers=agent,
        timeout=15, trust_env=False,
    ).json()
    item2 = next(i for i in queues2["items"] if i["id"] == cid)
    assert item2["unread"] == 0


def test_knowledge_publish_permission(stack):  # noqa: ANN001
    agent = _login(stack, "agent")
    supervisor = _login(stack, "supervisor")
    # agent 只能 draft / submitted
    for status in ("draft", "submitted"):
        r = httpx.post(
            _console(stack) + "/api/knowledge", headers=agent,
            json={"title": "权限测试", "content": "内容", "status": status},
            timeout=15, trust_env=False,
        )
        assert r.status_code == 201, (status, r.text)
    denied = httpx.post(
        _console(stack) + "/api/knowledge", headers=agent,
        json={"title": "权限测试", "content": "内容", "status": "published"},
        timeout=15, trust_env=False,
    )
    assert denied.status_code == 403
    assert denied.json()["error"] == "supervisor_required"
    # supervisor 可发布
    ok = httpx.post(
        _console(stack) + "/api/knowledge", headers=supervisor,
        json={"title": "权限测试", "content": "内容", "status": "published"},
        timeout=15, trust_env=False,
    )
    assert ok.status_code == 201
    assert ok.json()["status"] == "published"
    # bad status
    bad = httpx.post(
        _console(stack) + "/api/knowledge", headers=supervisor,
        json={"title": "x", "content": "y", "status": "live"},
        timeout=15, trust_env=False,
    )
    assert bad.status_code == 400
    # customer 全部拒绝
    customer = _login(stack, "customer", surface="customer")
    forbidden = httpx.post(
        _console(stack) + "/api/knowledge", headers=customer,
        json={"title": "x", "content": "y", "status": "draft"},
        timeout=15, trust_env=False,
    )
    assert forbidden.status_code == 403


def test_role_scoping_and_cross_tenant(stack):  # noqa: ANN001
    finance = _login(stack, "finance")
    for path in ("/api/workbench/queues", "/api/conversations",
                 "/api/agents", "/api/stats/summary"):
        denied = httpx.get(
            _console(stack) + path, headers=finance, timeout=15, trust_env=False,
        )
        assert denied.status_code == 403, path
    approvals = httpx.get(
        _console(stack) + "/api/approvals", headers=finance,
        timeout=15, trust_env=False,
    )
    assert approvals.status_code == 200
    # 跨租户：会话 404、队列为空
    result, _ = _send_customer(stack, "你好")
    cid = result["conversation_id"]
    other = _login(stack, "agent2", tenant="t_other")
    missing = httpx.get(
        _console(stack) + f"/api/conversations/{cid}/summary", headers=other,
        timeout=15, trust_env=False,
    )
    assert missing.status_code == 404
    empty = httpx.get(
        _console(stack) + "/api/workbench/queues?tab=pending", headers=other,
        timeout=15, trust_env=False,
    )
    assert empty.json()["counts"]["pending"] == 0


def test_agent_sees_only_own_scope(stack):  # noqa: ANN001
    """坐席队列只含未分配 + 自己接管；主管全租户。"""
    result, _ = _send_customer(stack, "转人工")
    cid = result["conversation_id"]
    agent = _login(stack, "agent")
    httpx.post(
        _console(stack) + f"/api/conversations/{cid}/takeover", headers=agent,
        timeout=30, trust_env=False,
    )
    supervisor = _login(stack, "supervisor")
    # 把会话转交给 supervisor：agent 不再在 human tab 看到它
    transferred = httpx.post(
        _console(stack) + f"/api/conversations/{cid}/transfer", headers=agent,
        json={"to_agent_id": "SUP-001"}, timeout=30, trust_env=False,
    )
    assert transferred.status_code == 200
    # 已分配给 supervisor 的会话不出现在 agent 的 human tab
    agent_human = httpx.get(
        _console(stack) + "/api/workbench/queues?tab=human", headers=agent,
        timeout=15, trust_env=False,
    ).json()
    assert all(i["id"] != cid for i in agent_human["items"])
    supervisor_human = httpx.get(
        _console(stack) + "/api/workbench/queues?tab=human", headers=supervisor,
        timeout=15, trust_env=False,
    ).json()
    assert any(i["id"] == cid for i in supervisor_human["items"])


def test_assist_and_tools_bound_to_conversation(stack):  # noqa: ANN001
    """坐席 AI 查询：绑定会话后能查订单/客户；工具面板调用成功；客户角色拒绝。"""
    result, _ = _send_customer(stack, "我的授信额度")
    cid = result["conversation_id"]
    agent = _login(stack, "agent")
    # 该测试栈只起 customer 域（不含 order），用授信查询验证“绑定会话后可用”
    ask = httpx.post(
        _console(stack) + "/api/assist/ask", headers=agent,
        json={"question": "我的授信额度", "conversation_id": cid},
        timeout=90, trust_env=False,
    )
    assert ask.status_code == 200, ask.text
    reply = ask.json()["reply"]
    assert "授信额度" in reply and "50000" in reply
    for internal in ("tenant_id", "customer_id", "tool_events"):
        assert internal not in reply, internal

    tools = httpx.get(
        _console(stack) + "/api/tools", headers=agent,
        timeout=15, trust_env=False,
    )
    assert tools.status_code == 200
    tool_list = tools.json()
    ids = {t["id"] for t in tool_list}
    assert {"customer.query", "product.lookup", "order.query"} <= ids
    requires = {t["id"]: t.get("requires_customer") for t in tool_list}
    assert requires["order.query"] is True
    assert requires["fulfill.track"] is True
    assert requires["ticket.query"] is True
    assert requires["product.lookup"] is False
    assert requires["customer.query"] is False

    invoked = httpx.post(
        _console(stack) + "/api/tools/customer.query/invoke", headers=agent,
        json={"params": {"query": "CU-1001"}, "conversation_id": cid},
        timeout=60, trust_env=False,
    )
    assert invoked.status_code == 200, invoked.text
    body = invoked.json()
    assert body["state"] == "TASK_STATE_COMPLETED"
    assert "王小明" in str(body.get("data") or body.get("text"))

    customer = _login(stack, "customer", surface="customer")
    denied = httpx.post(
        _console(stack) + "/api/assist/ask", headers=customer,
        json={"question": "查询订单 SO-2001", "conversation_id": cid},
        timeout=15, trust_env=False,
    )
    assert denied.status_code == 403
