"""安全加固单测：JWT / 租户隔离 / PII / 限流 / 审批双人复核 / 统一审计。"""

import json
import sqlite3
from pathlib import Path

import pytest
from langchain_core.messages import HumanMessage

from console import db as console_db
from domains.aftersale.tools import judge_eligibility, query_ticket
from domains.customer.tools import customer_search
from domains.finance.tools import invoice_issue, refund_settle
from domains.merchandising.tools import product_lookup
from domains.order.tools import order_amend, order_query
from domains.policy.tools import answer_policy
from orchestrator.graph import intake, rule_understand
from orchestrator.route_table import route_plan
from shared import approvals, ratelimit, security_db
from shared.audit import audit_event, list_events, verify_chain
from shared.auth import (
    ALL_SCOPES,
    AuthError,
    ROLE_SCOPES,
    auth_config,
    create_token,
    decode_token,
    ensure_scope,
    set_current_auth,
)
from shared.pii import mask_pii

MOCK_DB = Path("infra/mockdata/mock.db")


@pytest.fixture(autouse=True)
def _shared_and_mock(tmp_path, monkeypatch):
    monkeypatch.setenv("SHARED_DB_PATH", str(tmp_path / "shared.db"))
    security_db.reset_for_tests(str(tmp_path / "shared.db"))
    set_current_auth(None)
    from scripts.seed_mock import seed

    seed()
    yield
    seed()
    set_current_auth(None)
    security_db.reset_for_tests()


# ---------------------------------------------------------------------------
# JWT / scope
# ---------------------------------------------------------------------------

def test_jwt_roundtrip_and_tamper():
    token = create_token(
        tenant_id="t_demo", actor_id="CU-1001", role="customer",
        scopes=["order.read", "finance.read"],
    )
    ctx = decode_token(token)
    assert ctx.tenant_id == "t_demo"
    assert ctx.actor_id == "CU-1001"
    assert ctx.role == "customer"
    assert ctx.has_scope("order.read")
    assert not ctx.has_scope("order.write")

    bad = token[:-1] + ("a" if token[-1] != "a" else "b")
    with pytest.raises(AuthError):
        decode_token(bad)

    expired = create_token(
        tenant_id="t_demo", actor_id="x", role="service",
        scopes=ALL_SCOPES, ttl_seconds=-1,
    )
    with pytest.raises(AuthError):
        decode_token(expired)


def test_scope_default_deny_and_write_covers_read():
    assert ensure_scope({}, "order.read") is False
    assert ensure_scope(auth_config("order.read"), "order.read") is True
    assert ensure_scope(auth_config("order.write"), "order.read") is True
    assert ensure_scope(auth_config("order.read"), "order.write") is False


# ---------------------------------------------------------------------------
# 租户隔离（所有域）
# ---------------------------------------------------------------------------

def test_tenant_isolation_across_domains():
    demo = auth_config("merchandising.read", "order.read", "aftersale.read",
                       "policy.read")
    demo["metadata"]["customer_id"] = "CU-1001"
    other = auth_config("merchandising.read", "order.read", "aftersale.read",
                        "policy.read", tenant_id="t_other")
    other["metadata"]["customer_id"] = "CU-2001"

    p_demo = json.loads(product_lookup.invoke({"sku": "SKU-A001"}, config=demo))
    p_other = json.loads(product_lookup.invoke({"sku": "SKU-A001"}, config=other))
    assert p_demo["tenant_id"] == "t_demo" and p_other["tenant_id"] == "t_other"

    o_demo = json.loads(order_query.invoke({"order_no": "SO-2001"}, config=demo))
    o_other = json.loads(order_query.invoke({"order_no": "SO-2001"}, config=other))
    assert o_demo["customer_id"] == "CU-1001" and o_demo["amount"] == 199.0
    assert o_other["customer_id"] == "CU-2001" and o_other["amount"] == 299.0

    t_demo = json.loads(query_ticket.invoke({"ticket_no": "TK-3001"}, config=demo))
    t_other = json.loads(query_ticket.invoke({"ticket_no": "TK-3001"}, config=other))
    assert t_demo["customer_id"] == "CU-1001"
    assert t_other["customer_id"] == "CU-2001"

    assert json.loads(answer_policy.invoke(
        {"query": "保修"}, config=demo))["clauses"]
    assert json.loads(answer_policy.invoke(
        {"query": "保修"}, config=other))["clauses"]

    assert json.loads(customer_search.invoke(
        {"query": "王小明"}, config=auth_config("customer.read")))
    assert json.loads(customer_search.invoke(
        {"query": "王小明"},
        config=auth_config("customer.read", tenant_id="t_other"))) == []


def test_console_conversation_tenant_isolation(tmp_path, monkeypatch):
    monkeypatch.setattr(console_db, "DB_PATH", str(tmp_path / "console.db"))
    monkeypatch.setattr(console_db, "_conn", None)
    try:
        c_demo = console_db.create_conversation("CU-1001", tenant_id="t_demo")
        c_other = console_db.create_conversation("CU-2001", tenant_id="t_other")
        assert console_db.get_conversation(c_demo["id"], tenant_id="t_other") is None
        assert console_db.get_conversation(c_other["id"], tenant_id="t_demo") is None
        assert {c["id"] for c in console_db.list_conversations(tenant_id="t_demo")} == {
            c_demo["id"]
        }
        assert {c["id"] for c in console_db.list_conversations(tenant_id="t_other")} == {
            c_other["id"]
        }
    finally:
        console_db._conn = None


# ---------------------------------------------------------------------------
# PII
# ---------------------------------------------------------------------------

def test_pii_masking():
    text = "手机 13812345678，身份证 110101199001011234，邮箱 a@b.com，卡 6222021234567890123"
    masked, findings = mask_pii(text)
    assert "13812345678" not in masked
    assert "110101199001011234" not in masked
    assert "a@b.com" not in masked
    assert "6222021234567890123" not in masked
    assert {f.kind for f in findings} >= {"phone", "id_card", "email", "bank_card"}


def test_intake_masks_pii_before_state():
    state = {"messages": [HumanMessage(content="我的手机号 13812345678，帮我查订单")]}
    cfg = auth_config("orchestrator.read", role="customer", actor_id="CU-1001")
    cfg["metadata"].update({"context_id": "c1", "trace_id": "t1"})
    out = intake(state, cfg)
    assert "13812345678" not in out["user_text"]
    assert out["pii_masked"] >= 1
    assert out["tenant_id"] == "t_demo"
    assert out["actor_id"] == "CU-1001"


# ---------------------------------------------------------------------------
# 限流
# ---------------------------------------------------------------------------

def test_rate_limit_sliding_window():
    ratelimit.reset()
    assert ratelimit.check("k", limit=2)[0] is True
    assert ratelimit.check("k", limit=2)[0] is True
    allowed, retry = ratelimit.check("k", limit=2)
    assert allowed is False and retry >= 1
    ratelimit.reset()
    assert ratelimit.check("k", limit=2)[0] is True


# ---------------------------------------------------------------------------
# 高价值动作：审批 + 双人复核
# ---------------------------------------------------------------------------

def _insert_high_value_order(order_no: str = "SO-9001") -> None:
    conn = sqlite3.connect(MOCK_DB)
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO orders(tenant_id, order_no, customer_id, sku, qty, "
            "amount, status, created_at, paid_at, pay_channel, address) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("t_demo", order_no, "CU-1001", "SKU-A003", 1, 6000.0, "signed",
             "2026-10-01 10:00:00", "2026-10-01 10:01:00", "alipay", "杭州市"),
        )
    conn.close()


def test_high_value_invoice_requires_dual_control():
    _insert_high_value_order()
    requester = auth_config(
        "finance.write", "finance.read", role="customer", actor_id="CU-1001"
    )
    requester["metadata"]["context_id"] = "ctx-high"

    preview = json.loads(invoice_issue.invoke(
        {"order_no": "SO-9001", "title": "测试公司"}, config=requester
    ))
    assert preview["status"] == "pending_approval"
    assert preview["required_approvals"] == 2 and preview["dual_control"] is True
    approval_id = preview["approval_id"]

    # 申请人不能自批
    self_approve = approvals.approve(
        "t_demo", approval_id, actor_id="CU-1001", role="customer"
    )
    assert self_approve["error"] == "self_approval_forbidden"

    # 只批一票仍 pending（需要 finance + supervisor/admin）
    first = approvals.approve(
        "t_demo", approval_id, actor_id="SUP-001", role="supervisor"
    )
    assert first["status"] == "pending"
    second = approvals.approve(
        "t_demo", approval_id, actor_id="FIN-001", role="finance"
    )
    assert second["status"] == "approved"

    # 审批通过后带 approval_id 执行
    done = json.loads(invoice_issue.invoke(
        {"order_no": "SO-9001", "title": "测试公司", "approval_id": approval_id},
        config=requester,
    ))
    assert done["status"] == "issued" and done["verified"] is True
    assert approvals.get_approval("t_demo", approval_id)["status"] == "executed"

    actions = {e["action"] for e in list_events("t_demo")}
    assert {"approval.request", "approval.approve", "approval.execute",
            "tool.invoice_issue"} <= actions


def test_high_value_price_change_dual_control_and_retry():
    conn = sqlite3.connect(MOCK_DB)
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO orders(tenant_id, order_no, customer_id, sku, qty, "
            "amount, status, created_at, paid_at, pay_channel, address) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("t_demo", "SO-9002", "CU-1001", "SKU-A003", 1, 2399.0, "pending_payment",
             "2026-10-01 10:00:00", "", "", "杭州市"),
        )
    conn.close()
    requester = auth_config("order.write", "order.read", role="agent", actor_id="AG-001")
    requester["metadata"]["context_id"] = "ctx-price"
    requester["metadata"]["customer_id"] = "CU-1001"

    preview = json.loads(order_amend.invoke(
        {"order_no": "SO-9002", "address": "", "new_price": 6000.0},
        config=requester,
    ))
    assert preview["status"] == "pending_approval"
    assert preview["required_approvals"] == 2 and preview["dual_control"] is True
    approval_id = preview["approval_id"]

    approvals.approve("t_demo", approval_id, actor_id="SUP-001", role="supervisor")
    approvals.approve("t_demo", approval_id, actor_id="FIN-001", role="finance")

    # 审批后重发原请求：自动找到已批准单并消费，不需要在聊天里传 approval_id
    done = json.loads(order_amend.invoke(
        {"order_no": "SO-9002", "address": "", "new_price": 6000.0},
        config=requester,
    ))
    assert done["status"] == "amended" and done["amount"] == 6000.0
    assert approvals.get_approval("t_demo", approval_id)["status"] == "executed"


def test_customer_cannot_create_approval_on_other_order():
    """越权改价必须在 assess/create_approval 之前被对象归属拦截。"""
    cfg = auth_config(
        *ROLE_SCOPES["customer"], role="customer", actor_id="CU-1001"
    )
    out = json.loads(order_amend.invoke(
        {"order_no": "SO-2002", "address": "", "new_price": 6000.0},
        config=cfg,
    ))
    assert out["error"] == "not_found"
    assert approvals.list_approvals("t_demo", status="pending") == []


def test_price_change_route_is_deterministic():
    u = rule_understand("把 SO-2001 改价到 6000")
    assert u["intent"] == "order_price_amend"
    assert u["slots"]["order_no"] == "SO-2001"
    assert u["slots"]["new_price"] == 6000.0
    plan = route_plan("order_price_amend", {"order_no": "SO-9002",
                                            "new_price": 6000}, 0.9)
    assert plan is not None
    assert plan["domains"] == ["order"] and plan["mode"] == "single"


def test_low_value_refund_requires_approval():
    """退款属高风险资金动作：金额 199（低于旧阈值 500）也必须人工审批。"""
    cfg = auth_config("finance.write", role="customer", actor_id="CU-1001")
    cfg["metadata"]["context_id"] = "ctx-low"
    out = json.loads(refund_settle.invoke(
        {"order_no": "SO-2001"}, config=cfg
    ))
    assert out["status"] == "pending_approval"
    assert out["approval_id"]
    assert out["required_approvals"] == 1
    # 未审批前不得落退款单
    conn = sqlite3.connect("infra/mockdata/mock.db")
    try:
        n = conn.execute(
            "SELECT count(*) FROM refunds WHERE tenant_id='t_demo' "
            "AND order_no='SO-2001'").fetchone()[0]
    finally:
        conn.close()
    assert n == 0
    # 关闭审批单，避免污染后续 pending 断言
    approvals.reject("t_demo", out["approval_id"], actor_id="FIN-001",
                     role="finance", reason="policy test cleanup")


# ---------------------------------------------------------------------------
# 统一审计：哈希链 + PII 不入审计
# ---------------------------------------------------------------------------

def test_shared_audit_chain_and_pii_redaction():
    audit_event(
        tenant_id="t_demo", actor_id="CU-1001", actor_role="customer",
        action="tool.call", target="order.query",
        params={"phone": "13812345678"}, result_summary="ok",
    )
    events = list_events("t_demo")
    assert events[0]["params"]["phone"] != "13812345678"
    assert verify_chain("t_demo")["ok"] is True

    conn = security_db.get_conn()
    with conn:
        conn.execute("UPDATE audit_events SET result_summary='tampered' WHERE id=1")
    assert verify_chain("t_demo")["ok"] is False


# ---------------------------------------------------------------------------
# 登录（控制台用户表）
# ---------------------------------------------------------------------------

def test_console_login_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr(console_db, "DB_PATH", str(tmp_path / "console.db"))
    monkeypatch.setattr(console_db, "_conn", None)
    try:
        user = console_db.authenticate("t_demo", "customer", "demo123")
        assert user and user["user_id"] == "CU-1001"
        assert user["role"] == "customer"
        assert console_db.authenticate("t_demo", "customer", "wrong") is None
        assert console_db.authenticate("t_other", "customer", "demo123") is None
        assert console_db.authenticate("t_other", "customer2", "demo123")
    finally:
        console_db._conn = None
