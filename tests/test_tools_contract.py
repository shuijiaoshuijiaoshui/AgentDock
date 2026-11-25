"""商品域工具合同测试：Mock 数据 + scope 矩阵 + 越权用例（技术方案 9 章）。"""

import json
from types import SimpleNamespace

import pytest
from a2a.types import TaskState

from domains.merchandising.tools import (
    compare_matrix,
    product_lookup,
    product_retrieve,
    promo_evaluate,
)
from shared.auth import auth_config

CFG_OK = auth_config("merchandising.read")
CFG_BAD = auth_config("finance.read")


def test_product_lookup_hit():
    out = json.loads(product_lookup.invoke({"sku": "SKU-A001"}, config=CFG_OK))
    assert out["name"] == "无线鼠标 Pro"
    assert out["price"] == 199.0


def test_product_lookup_miss():
    out = json.loads(product_lookup.invoke({"sku": "SKU-X999"}, config=CFG_OK))
    assert out["error"] == "not_found"


def test_scope_mismatch_rejected():
    """越权：scope 不匹配 → 结构化 rejected，不进 LLM 上下文（8.2）。"""
    out = json.loads(product_lookup.invoke({"sku": "SKU-A001"}, config=CFG_BAD))
    assert out["error"] == "rejected"
    assert out["need_scope"] == "merchandising.read"


def test_product_retrieve_keyword():
    out = json.loads(product_retrieve.invoke({"keywords": "鼠标"}, config=CFG_OK))
    assert isinstance(out, list) and len(out) == 1
    assert out[0]["sku"] == "SKU-A001"


def test_compare_matrix():
    out = json.loads(
        compare_matrix.invoke({"skus": "SKU-A001,SKU-A002"}, config=CFG_OK)
    )
    assert len(out["matrix"]) == 2
    names = {r["name"] for r in out["matrix"]}
    assert names == {"无线鼠标 Pro", "机械键盘 K870"}


def test_compare_matrix_needs_two():
    out = json.loads(compare_matrix.invoke({"skus": "SKU-A001"}, config=CFG_OK))
    assert out["error"] == "need_two_skus"


def test_promo_evaluate_estimate():
    """SKU-A002：499 × 9 折 − 20 全场券 = 429.10，且必须标注估算。"""
    out = json.loads(promo_evaluate.invoke({"sku": "SKU-A002"}, config=CFG_OK))
    assert out["estimate"] is True
    assert out["estimate_price"] == 429.1
    assert "9 折" in out["applied"]


@pytest.mark.asyncio
async def test_console_tool_render_uses_declared_param(monkeypatch):
    """工具面板传了 customer_id 上下文时，render 仍必须用工具自己的参数。"""
    from console import tools as console_tools

    captured = {}

    async def fake_delegate(url, text, **kwargs):  # noqa: ANN001, ANN003
        captured["text"] = text
        return SimpleNamespace(
            state=TaskState.TASK_STATE_COMPLETED,
            text="ok",
            data_parts=[{"data": {"clause_id": "CLS-001"}}],
        )

    monkeypatch.setattr("shared.a2a_client.delegate", fake_delegate)
    monkeypatch.setattr("shared.audit.audit_from_auth", lambda *a, **k: None)

    from shared.auth import AuthContext

    auth = AuthContext(tenant_id="t_demo", actor_id="AG-001", role="agent",
                       scopes=("policy.read",))
    out = await console_tools.invoke(
        "policy.clause",
        {"customer_id": "CU-1001", "clause_id": "CLS-001"},
        "", auth=auth,
    )
    assert out["state"] == "TASK_STATE_COMPLETED"
    assert "CLS-001" in captured["text"]
    assert "CU-1001" not in captured["text"]


# ---- 国补资格（subsidy.check）：价格差异只来自活动/国补，无会员制度 ----

def _cust_cfg(actor: str) -> dict:
    return auth_config("merchandising.read", role="customer", actor_id=actor)


def _staff_cfg(customer_id: str) -> dict:
    cfg = auth_config("merchandising.read", role="agent", actor_id="AG-001")
    cfg["metadata"]["customer_id"] = customer_id
    return cfg


def test_subsidy_eligible_and_best_rule_wins():
    from domains.merchandising.tools import subsidy_check

    # SKU-A003：SUB-01(15%=359.85) vs SUB-02(20%=479.8) → 取更优 SUB-02
    out = json.loads(subsidy_check.invoke({"sku": "SKU-A003"}, config=_cust_cfg("CU-1002")))
    assert out["eligible"] is True
    assert out["subsidy_id"] == "SUB-02"
    assert out["subsidy_amount"] == 479.8
    assert out["final_price"] == 1919.2
    assert out["estimate"] is True


def test_subsidy_expired_rule_ignored():
    from domains.merchandising.tools import subsidy_check

    # SUB-03（25%）已于 2025-12-31 过期，绝不能被返回
    out = json.loads(subsidy_check.invoke({"sku": "SKU-A002"}, config=_cust_cfg("CU-1002")))
    assert out["subsidy_id"] == "SUB-01"
    assert out["subsidy_amount"] == 74.85


def test_subsidy_per_user_limit_enforced():
    from domains.merchandising.tools import subsidy_check

    # CU-1001 已经买过 SKU-A001（SO-2001），限购 1 件 → 不可再享
    out = json.loads(subsidy_check.invoke({"sku": "SKU-A001"}, config=_cust_cfg("CU-1001")))
    assert out["eligible"] is False
    assert "限购" in out["reason"]
    assert out["purchased_qty"] == 1


def test_subsidy_staff_with_customer_context():
    from domains.merchandising.tools import subsidy_check

    # 坐席代查：metadata.customer_id 决定限购口径
    out = json.loads(subsidy_check.invoke({"sku": "SKU-A002"}, config=_staff_cfg("CU-1001")))
    assert out["eligible"] is True


def test_subsidy_scope_mismatch_and_not_found():
    from domains.merchandising.tools import subsidy_check

    denied = json.loads(subsidy_check.invoke({"sku": "SKU-A002"}, config=CFG_BAD))
    assert denied["error"] == "rejected"
    missing = json.loads(subsidy_check.invoke(
        {"sku": "SKU-X999"}, config=_cust_cfg("CU-1002")))
    assert missing["error"] == "not_found"
