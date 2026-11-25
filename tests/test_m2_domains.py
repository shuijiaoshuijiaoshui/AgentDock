"""M2 单元/合同测试：政策检索内核、两域工具、路由表新意图（AC-21）。"""

import importlib.util
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

# 种子 Mock 数据（policy_docs/aftersales/tickets）
spec = importlib.util.spec_from_file_location(
    "seed_mock", Path(__file__).resolve().parents[1] / "scripts" / "seed_mock.py"
)
_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(_mod)
_mod.seed()

from domains.aftersale.tools import (  # noqa: E402
    create_ticket,
    determine_responsibility,
    judge_eligibility,
    query_ticket,
)
from domains.policy.retriever import (  # noqa: E402
    get_effective_clause,
    search_clauses,
    search_knowledge,
)
from domains.policy.tools import answer_policy, get_clause  # noqa: E402
from orchestrator.route_table import route_plan  # noqa: E402
from shared.auth import auth_config  # noqa: E402

CFG = auth_config("aftersale.read")
CFG["configurable"] = {"thread_id": "t"}
CFG["metadata"]["context_id"] = "test-ctx"
CFG["metadata"]["customer_id"] = "CU-1001"
CFG_WRITE = auth_config("aftersale.write")
CFG_WRITE["configurable"] = {"thread_id": "t"}
CFG_WRITE["metadata"]["context_id"] = "test-ctx"
CFG_WRITE["metadata"]["customer_id"] = "CU-1001"
CFG_POLICY = auth_config("policy.read")
CFG_POLICY["configurable"] = {"thread_id": "t"}


# ---- 政策检索内核 ----

def test_effective_filter_returns_v2_only():
    """CLS-001 有 v1（已过期）/v2 两版：现行查询必须只回 v2（生效期硬过滤）。"""
    clause = get_effective_clause("CLS-001", tenant_id="t_demo")
    assert clause["version"] == "v2"
    assert clause["effective_range"].startswith("2025-07-01")


def test_search_compound_chinese_query():
    """复合词"退货政策"经 2-gram 兜底命中 CLS-001 v2。"""
    hits = search_clauses("退货政策", tenant_id="t_demo")
    assert hits and hits[0]["clause_id"] == "CLS-001"
    assert hits[0]["version"] == "v2"


def test_search_missing_policy():
    assert search_clauses("量子纠缠", tenant_id="t_demo") == []


def test_compound_policy_question_does_not_misfire():
    """复合问题：正文里的“购买/商家”不得单独构成强证据；
    权威条款为空时由已发布知识兜底，并明确标注非权威。"""
    q = ("最近最新出的关于产品A的购买政策是什么?"
         "异地商家应该怎么操作区合作并且购买产品A")
    assert search_clauses(q, tenant_id="t_demo") == []
    ks = search_knowledge(q, tenant_id="t_demo")
    assert any(k["entry_id"] == "KB-1003" for k in ks)

    out = json.loads(answer_policy.invoke({"query": q}, config=CFG_POLICY))
    assert out["found"] is False
    assert out["clauses"] == []
    assert any(k["entry_id"] == "KB-1003" for k in out["knowledge"])
    assert "非权威" in out["answer"]
    assert "已找到生效条款" not in out["answer"]


def test_policy_tools_contract():
    out = json.loads(answer_policy.invoke({"query": "保修"}, config=CFG_POLICY))
    assert out["clauses"][0]["clause_id"] == "CLS-002"
    out2 = json.loads(get_clause.invoke({"clause_id": "CLS-001"}, config=CFG_POLICY))
    assert out2["version"] == "v2"
    out3 = json.loads(get_clause.invoke({"clause_id": "CLS-999"}, config=CFG_POLICY))
    assert out3["error"] == "not_found"


# ---- 售后工具 ----

def test_eligibility_in_warranty():
    out = json.loads(judge_eligibility.invoke(
        {"sku": "SKU-A001"}, config=CFG))
    assert out["eligible"] is True
    assert out["conclusion"] == "保修内"
    # 条款依据：政策服务在线时为 CLS-002 引用；离线时降级标注（两种都合法）
    assert ("CLS-002" in out["clause"]) or out["degraded"]


def test_eligibility_out_of_warranty():
    cfg = auth_config("aftersale.read")
    cfg["metadata"]["customer_id"] = "CU-1004"
    out = json.loads(judge_eligibility.invoke({"sku": "SKU-A002"}, config=cfg))
    assert out["eligible"] is False
    assert out["conclusion"].startswith("保修外")


def test_eligibility_no_record():
    out = json.loads(judge_eligibility.invoke({"sku": "SKU-ZZZ"}, config=CFG))
    assert out["error"] == "no_purchase_record"


def test_responsibility_rules():
    out = json.loads(determine_responsibility.invoke({"fault_type": "进水"}, config=CFG))
    assert out["responsibility"] == "buyer"
    out2 = json.loads(determine_responsibility.invoke({"fault_type": "物流破损"}, config=CFG))
    assert out2["responsibility"] == "carrier"
    out3 = json.loads(determine_responsibility.invoke({"fault_type": "按键失灵"}, config=CFG))
    assert out3["responsibility"] == "seller"


def test_ticket_create_and_query():
    """两阶段确认 + 幂等（R3/R5）：先拿 confirm_token，确认后才建单。"""
    args = {"sku": "SKU-A001", "fault_type": "按键失灵", "customer_id": "CU-1001"}
    preview = json.loads(create_ticket.invoke(args, config=CFG_WRITE))
    assert preview["status"] == "pending_confirm" and preview["confirm_token"]
    # 预览阶段不落库：biz_key 尚未写入
    assert preview["biz_key"] not in json.dumps(
        json.loads(query_ticket.invoke({"ticket_no": "TK-4001"}, config=CFG_WRITE)),
        ensure_ascii=False)

    out = json.loads(create_ticket.invoke(
        {**args, "confirm_token": preview["confirm_token"]}, config=CFG_WRITE))
    no = out["ticket_no"]
    assert out["created"] is True and out["verified"] is True
    assert "compensation" in out  # 补偿语义在位

    # 幂等：同参数再调一次 → 返回同一单号，不新建
    again = json.loads(create_ticket.invoke(
        {**args, "confirm_token": preview["confirm_token"]}, config=CFG_WRITE))
    assert again["ticket_no"] == no and again["idempotent"] is True

    got = json.loads(query_ticket.invoke({"ticket_no": no}, config=CFG_WRITE))
    assert got["ticket_no"] == no and got["fault_type"] == "按键失灵"


def test_ticket_confirm_token_mismatch():
    """R3：confirm_token 不匹配必须拒绝（防伪造确认）。"""
    out = json.loads(create_ticket.invoke(
        {"sku": "SKU-A001", "fault_type": "按键失灵", "customer_id": "CU-1001",
         "confirm_token": "forged-token"}, config=CFG_WRITE))
    assert out["error"] == "invalid_confirm_token"


def test_ticket_write_requires_write_scope():
    """R1：写操作只带 read scope 必须被拒（默认拒绝）。"""
    args = {"sku": "SKU-A009", "fault_type": "测试", "customer_id": "CU-1001"}
    out = json.loads(create_ticket.invoke(args, config=CFG))  # read scope
    assert out["error"] == "rejected" and out["need_scope"] == "aftersale.write"


def test_tools_reject_without_scope():
    """R1：metadata 未带 scope 一律拒绝（原实现是默认放行，审计发现并修复）。"""
    cfg_none = {"configurable": {"thread_id": "t"}}
    assert json.loads(judge_eligibility.invoke(
        {"sku": "SKU-A001", "buy_date": ""}, config=cfg_none))["error"] == "rejected"
    assert json.loads(answer_policy.invoke(
        {"query": "保修"}, config=cfg_none))["error"] == "rejected"


# ---- 路由表新意图（AC-21 确定性） ----

def test_warranty_check_routes_three_domains_parallel():
    expected = route_plan("warranty_check", {"sku": "SKU-A001"}, 0.9)
    assert expected is not None
    assert expected["mode"] == "parallel"
    assert set(expected["domains"]) == {"aftersale", "policy", "merchandising"}
    for _ in range(100):
        assert route_plan("warranty_check", {"sku": "SKU-A001"}, 0.9) == expected


@pytest.mark.parametrize("intent,domain", [
    ("policy_qa", "policy"),
    ("aftersale_eligibility", "aftersale"),
    ("ticket_query", "aftersale"),
])
def test_single_domain_new_intents(intent, domain):
    plan = route_plan(intent, {}, 0.9)
    assert plan is not None and plan["domains"] == [domain] and plan["mode"] == "single"
