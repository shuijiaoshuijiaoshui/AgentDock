"""财务域工具合同测试：账单权威、退款幂等、chaos 注入、两阶段开票。"""

import json
import sqlite3

import pytest

from domains.finance.tools import (
    billing_statement,
    invoice_issue,
    refund_settle,
    refund_status_query,
)
from domains.order.tools import restore_order
from shared import approvals
from shared.auth import auth_config

MOCK_DB = "infra/mockdata/mock.db"
CFG_READ = auth_config("finance.read")
CFG_READ["metadata"]["context_id"] = "t1"
CFG_READ["metadata"]["customer_id"] = "CU-1001"
CFG_WRITE = auth_config("finance.write")
CFG_WRITE["metadata"]["context_id"] = "t1"
CFG_WRITE["metadata"]["customer_id"] = "CU-1001"
CFG_CHAOS = auth_config("finance.write")
CFG_CHAOS["metadata"]["context_id"] = "t1"
CFG_CHAOS["metadata"]["customer_id"] = "CU-1001"
CFG_CHAOS["metadata"]["chaos"] = "finance_settle_fail"
CFG_BAD = auth_config("merchandising.read")


def _cfg(scope: str, customer_id: str) -> dict:
    cfg = auth_config(scope)
    cfg["metadata"]["context_id"] = "t1"
    cfg["metadata"]["customer_id"] = customer_id
    return cfg


def _cleanup():
    conn = sqlite3.connect(MOCK_DB)
    with conn:
        conn.execute("DELETE FROM refunds WHERE biz_key LIKE 't1:%'")
        conn.execute("DELETE FROM invoices WHERE biz_key LIKE 't1:%'")
        conn.execute(
            "DELETE FROM order_cancel_log WHERE biz_key LIKE 't1:%'")
        # 本地 dev 栈可能已写过 SO-2001：按单据清，保证用例独立
        conn.execute(
            "DELETE FROM refunds WHERE tenant_id='t_demo' AND order_no='SO-2001'")
        conn.execute(
            "DELETE FROM invoices WHERE tenant_id='t_demo' AND order_no='SO-2001'")
        conn.execute(
            "DELETE FROM order_cancel_log WHERE tenant_id='t_demo' "
            "AND order_no='SO-2001'")
        conn.execute("UPDATE orders SET status='signed' WHERE tenant_id='t_demo' AND order_no='SO-2001'")


@pytest.fixture(autouse=True)
def _clean_finance_db():
    """前置清理：避免本地 dev 栈/上一次用例留下的退款单影响断言。"""
    _cleanup()
    yield
    _cleanup()


# ---------------------------- 账单（退款金额权威源） ----------------------------

def test_billing_hit():
    out = json.loads(billing_statement.invoke({"order_no": "SO-2001"}, config=CFG_READ))
    assert out["amount"] == 199.0
    assert out["pay_channel"] == "alipay"


def test_billing_unpaid_rejected():
    """未支付订单无账单——财务口径：无数据即拒答，不推测（3.4）。"""
    out = json.loads(billing_statement.invoke({"order_no": "SO-2004"}, config=_cfg("finance.read", "CU-1004")))
    assert out["error"] == "unpaid"


def test_billing_scope_rejected():
    out = json.loads(billing_statement.invoke({"order_no": "SO-2001"}, config=CFG_BAD))
    assert out["error"] == "rejected" and out["need_scope"] == "finance.read"


# ---------------------------- 退款执行（幂等 + chaos） ----------------------------

def test_refund_settle_and_idempotent():
    # 退款一律审批：先拿待审批单，人工批准后再执行
    preview = json.loads(refund_settle.invoke(
        {"order_no": "SO-2001"}, config=CFG_WRITE))
    assert preview["status"] == "pending_approval"
    approval_id = preview["approval_id"]
    approved = approvals.approve(
        "t_demo", approval_id, actor_id="FIN-001", role="finance")
    assert approved.get("status") == "approved"

    first = json.loads(refund_settle.invoke(
        {"order_no": "SO-2001", "approval_id": approval_id}, config=CFG_WRITE))
    assert first["status"] == "settled" and first["amount"] == 199.0
    assert first["idempotent"] is False and first["verified"] is True

    again = json.loads(refund_settle.invoke({"order_no": "SO-2001"}, config=CFG_WRITE))
    assert again["status"] == "settled"
    assert again["idempotent"] is True  # 同 biz_key 重放：未产生二次退款
    assert again["refund_id"] == first["refund_id"]


def test_refund_settle_chaos_injected():
    """chaos 注入 → 结构化失败（Saga 演练的故障注入点）。"""
    out = json.loads(refund_settle.invoke({"order_no": "SO-2001"}, config=CFG_CHAOS))
    assert out["error"] == "chaos_injected"
    # 故障注入不落数据
    conn = sqlite3.connect(MOCK_DB)
    n = conn.execute(
        "SELECT count(*) FROM refunds WHERE biz_key LIKE 't1:%'").fetchone()[0]
    assert n == 0


def test_refund_settle_unpaid_no_bill():
    out = json.loads(refund_settle.invoke({"order_no": "SO-2004"}, config=_cfg("finance.write", "CU-1004")))
    assert out["error"] == "no_bill"


def test_refund_status_query():
    out = json.loads(
        refund_status_query.invoke({"order_no": "SO-2003"}, config=_cfg("finance.read", "CU-1003")))
    assert out[0]["status"] == "settled"  # seed 预置


# ---------------------------- 开票（两阶段 + 幂等） ----------------------------

def test_invoice_two_phase_and_idempotent():
    preview = json.loads(invoice_issue.invoke(
        {"order_no": "SO-2001", "title": "测试公司"}, config=CFG_WRITE))
    assert preview["status"] == "pending_confirm"
    assert preview["preview"]["amount"] == 199.0

    done = json.loads(invoice_issue.invoke(
        {"order_no": "SO-2001", "title": "测试公司",
         "confirm_token": preview["confirm_token"]}, config=CFG_WRITE))
    assert done["status"] == "issued" and done["verified"] is True

    again = json.loads(invoice_issue.invoke(
        {"order_no": "SO-2001", "title": "测试公司",
         "confirm_token": preview["confirm_token"]}, config=CFG_WRITE))
    assert again["idempotent"] is True


def test_invoice_bad_token():
    out = json.loads(invoice_issue.invoke(
        {"order_no": "SO-2001", "title": "测试公司", "confirm_token": "wrong"},
        config=CFG_WRITE))
    assert out["error"] == "invalid_confirm_token"


def test_restore_order_refuses_after_refund():
    """已退款订单不允许 restore，避免“退款到账 + 订单恢复”的钱货两空。"""
    conn = sqlite3.connect(MOCK_DB)
    with conn:
        conn.execute(
            "UPDATE orders SET status='cancelled' "
            "WHERE tenant_id='t_demo' AND order_no='SO-2001'")
        conn.execute(
            "INSERT OR IGNORE INTO refunds(tenant_id, refund_id, order_no, biz_key, "
            "amount, status) VALUES ('t_demo','RF-GUARD','SO-2001','t1:SO-2001:refund',"
            "199.0,'settled')")
        conn.execute(
            "INSERT OR IGNORE INTO order_cancel_log(tenant_id, log_id, order_no, "
            "biz_key, prev_status) VALUES ('t_demo','CL-GUARD','SO-2001',"
            "'t1:SO-2001:cancel','signed')")
    cfg = _cfg("order.write", "CU-1001")
    out = json.loads(restore_order.invoke(
        {"biz_key": "t1:SO-2001:cancel"}, config=cfg))
    assert out["error"] == "refund_settled"
    conn = sqlite3.connect(MOCK_DB)
    status = conn.execute(
        "SELECT status FROM orders WHERE tenant_id='t_demo' AND order_no='SO-2001'"
    ).fetchone()[0]
    conn.close()
    assert status == "cancelled"


def test_refund_duplicate_request_shares_one_approval():
    """同订单重复申请退款 → 幂等返回同一张待审批单，不重复创建。"""
    conn = sqlite3.connect(
        __import__("shared.security_db", fromlist=["db_path"]).db_path())
    with conn:
        conn.execute(
            "DELETE FROM approvals WHERE tenant_id='t_demo' AND action='refund' "
            "AND target='SO-2001'")
    p1 = json.loads(refund_settle.invoke({"order_no": "SO-2001"}, config=CFG_WRITE))
    p2 = json.loads(refund_settle.invoke({"order_no": "SO-2001"}, config=CFG_WRITE))
    assert p1["status"] == "pending_approval"
    assert p2["status"] == "pending_approval"
    assert p2["approval_id"] == p1["approval_id"]
    assert p2.get("duplicate") is True
