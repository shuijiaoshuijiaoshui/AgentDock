"""售后资格：购买日期必须由 orders/aftersales 推导，用户自报不参与判定。"""

import json

import pytest

from domains.aftersale.tools import judge_eligibility
from scripts.seed_mock import seed
from shared.auth import auth_config


@pytest.fixture(autouse=True)
def _seed():
    seed()
    yield
    seed()


def _cfg(customer_id: str | None, scope: str = "aftersale.read") -> dict:
    cfg = auth_config(scope)
    if customer_id:
        cfg["metadata"]["customer_id"] = customer_id
    return cfg


def test_forged_buy_date_is_ignored():
    out = json.loads(judge_eligibility.invoke(
        {"sku": "SKU-A001", "buy_date": "2020-01-01"},
        config=_cfg("CU-1001"),
    ))
    assert out["buy_date_source"] == "orders.paid_at"
    assert out["buy_date"].startswith("2026-06-15")
    assert out["eligible"] is True
    assert out["ignored_user_buy_date"] is True
    assert out["buy_date_unverified"] == "2020-01-01"


def test_cannot_use_another_customers_purchase_record():
    out = json.loads(judge_eligibility.invoke(
        {"sku": "SKU-A001"}, config=_cfg("CU-1002")
    ))
    assert out["error"] == "no_purchase_record"


def test_cannot_reference_another_customers_order():
    out = json.loads(judge_eligibility.invoke(
        {"sku": "SKU-A001", "order_no": "SO-2001"}, config=_cfg("CU-1002")
    ))
    assert out["error"] == "not_found"


def test_out_of_warranty_uses_old_order_date():
    out = json.loads(judge_eligibility.invoke(
        {"sku": "SKU-A002", "order_no": "SO-2005"}, config=_cfg("CU-1004")
    ))
    assert out["buy_date"].startswith("2025-03-02")
    assert out["eligible"] is False
    assert out["conclusion"].startswith("保修外")


def test_unpaid_order_is_not_a_purchase_record():
    out = json.loads(judge_eligibility.invoke(
        {"sku": "SKU-B002", "order_no": "SO-2004"}, config=_cfg("CU-1004")
    ))
    assert out["error"] == "unpaid_order"


def test_sku_mismatch_is_rejected():
    out = json.loads(judge_eligibility.invoke(
        {"sku": "SKU-A002", "order_no": "SO-2001"}, config=_cfg("CU-1001")
    ))
    assert out["error"] == "sku_mismatch"


def test_no_purchase_record():
    out = json.loads(judge_eligibility.invoke(
        {"sku": "SKU-ZZZ"}, config=_cfg("CU-1001")
    ))
    assert out["error"] == "no_purchase_record"


def test_staff_must_pass_customer_id():
    out = json.loads(judge_eligibility.invoke(
        {"sku": "SKU-A001"}, config=_cfg(None)
    ))
    assert out["error"] == "customer_id_required"


def test_policy_tenant_isolation():
    cfg = auth_config("aftersale.read", tenant_id="t_other")
    cfg["metadata"]["customer_id"] = "CU-2001"
    out = json.loads(judge_eligibility.invoke({"sku": "SKU-A001"}, config=cfg))
    assert out["customer_id"] == "CU-2001"
    assert out["buy_date"].startswith("2026-07-01")
