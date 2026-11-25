"""客户域工具合同：资料/授信/对账/投诉 + tenant/归属校验。"""

import json
import sqlite3

import pytest

from domains.customer.tools import (
    complaint_list,
    credit_query,
    customer_profile,
    reconciliation_query,
)
from scripts.seed_mock import seed
from shared.auth import ROLE_SCOPES, auth_config


@pytest.fixture(autouse=True)
def _seed():
    seed()
    yield
    seed()


def _staff_cfg(customer_id: str | None, tenant: str = "t_demo") -> dict:
    cfg = auth_config("customer.read", role="agent", actor_id="AG-001",
                      tenant_id=tenant)
    if customer_id:
        cfg["metadata"]["customer_id"] = customer_id
    return cfg


def _cust_cfg(actor: str, tenant: str = "t_demo") -> dict:
    return auth_config("customer.read", role="customer", actor_id=actor,
                       tenant_id=tenant)


def test_profile_and_credit_for_own_customer():
    out = json.loads(customer_profile.invoke({}, config=_cust_cfg("CU-1001")))
    assert out["customer_id"] == "CU-1001"
    assert out["dealer_level"] == "金牌"
    assert out["customer_code"] == "DEALER-1001"
    assert "13800001234" not in json.dumps(out)  # phone 已脱敏

    credit = json.loads(credit_query.invoke({}, config=_cust_cfg("CU-1001")))
    assert credit["credit_limit"] == 50000.0
    assert credit["credit_used"] == 12000.0
    assert credit["credit_remaining"] == 38000.0
    assert credit["credit_days"] == 30


def test_reconciliation_and_complaint():
    recon = json.loads(reconciliation_query.invoke({}, config=_cust_cfg("CU-1001")))
    assert recon[0]["period"] == "2026-10"
    assert recon[0]["status"] == "matched"

    period = json.loads(reconciliation_query.invoke(
        {"period": "2026-10"}, config=_cust_cfg("CU-1001")))
    assert len(period) == 1
    empty = json.loads(reconciliation_query.invoke(
        {"period": "2020-01"}, config=_cust_cfg("CU-1001")))
    assert empty["error"] == "no_reconciliation"

    complaints = json.loads(complaint_list.invoke({}, config=_cust_cfg("CU-1003")))
    assert complaints["complaints"][0]["type"] == "物流延迟"
    none = json.loads(complaint_list.invoke({}, config=_cust_cfg("CU-1001")))
    assert none["complaints"] == []


def test_customer_cannot_read_other_customer():
    # customer 角色：即使 metadata 伪造 customer_id，也以 token.sub 为准
    cfg = _cust_cfg("CU-1001")
    cfg["metadata"]["customer_id"] = "CU-1002"
    out = json.loads(customer_profile.invoke({}, config=cfg))
    assert out["customer_id"] == "CU-1001"
    assert out["customer_code"] == "DEALER-1001"


def test_staff_without_customer_id_rejected():
    out = json.loads(credit_query.invoke({}, config=_staff_cfg(None)))
    assert out["error"] == "customer_id_required"


def test_staff_can_read_bound_customer():
    out = json.loads(customer_profile.invoke({}, config=_staff_cfg("CU-1001")))
    assert out["customer_id"] == "CU-1001"


def test_tenant_isolation():
    other = _cust_cfg("CU-2001", tenant="t_other")
    out = json.loads(credit_query.invoke({}, config=other))
    assert out["credit_limit"] == 60000.0  # t_other 自己的数据
    demo_missing = json.loads(reconciliation_query.invoke(
        {}, config=_staff_cfg("CU-1001", tenant="t_other")))
    assert demo_missing["error"] == "not_found"  # 客户不存在于该租户


def test_no_auth_rejected():
    out = json.loads(customer_profile.invoke({}, config={}))
    assert out["error"] == "rejected"


# ---- 升格工具：customer.search / customer.overview ----

def test_customer_search_by_id_name_phone():
    from domains.customer.tools import customer_search

    cfg = auth_config("customer.read", role="agent", actor_id="AG-001")
    by_id = json.loads(customer_search.invoke({"query": "CU-1001"}, config=cfg))
    assert by_id[0]["name"] == "王小明"
    by_name = json.loads(customer_search.invoke({"query": "李商户"}, config=cfg))
    assert by_name[0]["tier"] == "银牌"
    by_phone = json.loads(customer_search.invoke({"query": "9012"}, config=cfg))
    assert by_phone[0]["customer_id"] == "CU-1003"
    assert "1380000" not in json.dumps(by_id)  # 手机号脱敏


def test_customer_search_miss_and_empty():
    from domains.customer.tools import customer_search

    cfg = auth_config("customer.read", role="agent", actor_id="AG-001")
    assert json.loads(customer_search.invoke({"query": "不存在的人"}, config=cfg)) == []
    assert json.loads(customer_search.invoke(
        {"query": ""}, config=cfg))["error"] == "bad_request"


def test_customer_search_customer_role_denied():
    """IDOR 防线：customer 角色不能借 customer.search 枚举同租户其他客户。"""
    from domains.customer.tools import customer_search

    cfg = auth_config("customer.read", role="customer", actor_id="CU-1001")
    out = json.loads(customer_search.invoke({"query": "李商户"}, config=cfg))
    assert out["error"] == "not_found"


def test_customer_overview_staff_and_customer_roles():
    from domains.customer.tools import customer_overview

    staff = auth_config("customer.read", role="agent", actor_id="AG-001")
    staff["metadata"]["customer_id"] = "CU-1001"
    out = json.loads(customer_overview.invoke({}, config=staff))
    assert out["customer"]["customer_id"] == "CU-1001"
    assert out["orders"] and out["orders"][0]["order_no"]
    assert "tickets" in out and "reconciliations" in out

    cust = auth_config("customer.read", role="customer", actor_id="CU-1001")
    own = json.loads(customer_overview.invoke({}, config=cust))
    assert own["customer"]["customer_id"] == "CU-1001"
    # customer 角色请求他人 → not_found（resolve_customer 归属校验）
    forged = auth_config("customer.read", role="customer", actor_id="CU-1001")
    forged_out = json.loads(customer_overview.invoke(
        {"customer_id": "CU-1002"}, config=forged))
    assert forged_out["error"] == "not_found"
