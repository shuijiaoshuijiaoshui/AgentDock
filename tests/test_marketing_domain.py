"""营销经营分析域工具合同：投放/选品/竞品/爆款 + plan_save 两阶段 + 越权矩阵。

剧本（scripts/seed_mock.py _seed_marketing）：
- CP-01 ROI≈4.0 → 加投；CP-02 ROI≈1.4 → 保持观察；CP-03 ROI≈0.87 → 暂停/换素材；
- SKU-A003 销量指数增长（爆款）；SKU-B001 下滑；
- MP-02 竞品（SKU-B001 / COMP-9002 / 天猫）近 7 天降价 ≈9.9% → 提醒。
"""

import json

import pytest

from domains.marketing.tools import (
    ad_report,
    competitor_watch,
    hot_rank,
    plan_save,
    product_selection,
)
from scripts.seed_mock import seed
from shared.auth import ROLE_SCOPES, auth_config


@pytest.fixture(autouse=True)
def _seed():
    seed()
    yield
    seed()


def _staff_cfg(scope: str = "marketing.read", **kw) -> dict:
    return auth_config(scope, role="agent", actor_id="AG-001", **kw)


def _cust_cfg(actor: str, tenant: str = "t_demo") -> dict:
    return auth_config("marketing.read", role="customer", actor_id=actor,
                       tenant_id=tenant)


# ---- 越权矩阵 ----

def test_scope_mismatch_rejected():
    out = json.loads(ad_report.invoke({"query": ""}, config=_staff_cfg("finance.read")))
    assert out["error"] == "rejected"
    assert out["need_scope"] == "marketing.read"


def test_customer_role_denied_marketing_analysis():
    """marketing scope 不授予 customer 角色：即便伪造 token 带上 scope，
    经营分析也不该给 C 端出数（toB-only）。"""
    assert "marketing.read" not in ROLE_SCOPES["customer"]
    assert "marketing.write" not in ROLE_SCOPES["customer"]


# ---- 投放效果 + 方案 ----

def test_ad_report_roi_and_suggestions():
    out = json.loads(ad_report.invoke({"query": "近14天"}, config=_staff_cfg()))
    assert out["days"] == 14
    rois = {c["campaign_id"]: c for c in out["campaigns"]}
    assert rois["CP-01"]["roi"] >= 2
    assert "加投" in rois["CP-01"]["suggestion"]
    assert 1 <= rois["CP-02"]["roi"] < 2
    assert "保持观察" in rois["CP-02"]["suggestion"]
    assert rois["CP-03"]["roi"] < 1
    assert "暂停" in rois["CP-03"]["suggestion"]
    assert out["total"]["roi"] > 0
    assert {p["campaign_id"] for p in out["plan"]} == set(rois)


def test_ad_report_single_campaign_filter():
    out = json.loads(ad_report.invoke({"query": "CP-01 近7天"}, config=_staff_cfg()))
    assert len(out["campaigns"]) == 1
    assert out["campaigns"][0]["campaign_id"] == "CP-01"
    assert out["days"] == 7


# ---- 爆款 + 选品 ----

def test_hot_rank_growth_top():
    out = json.loads(hot_rank.invoke({"query": ""}, config=_staff_cfg()))
    assert out["days"] == 7
    top = out["top"]
    assert top, "应至少有一个爆款候选"
    assert top[0]["sku"] == "SKU-A003"  # 指数增长剧本
    assert top[0]["growth"] > 0
    assert top[0]["qty_now"] > top[0]["qty_prev"]
    # 扁平字段：编排层 ARBITRATE_INJECTIONS（hot_restock）注入履约域用
    assert out["top_sku"] == top[0]["sku"]


def test_product_selection_scores_and_reasons():
    out = json.loads(product_selection.invoke({"query": ""}, config=_staff_cfg()))
    cands = out["candidates"]
    assert cands
    scores = [c["score"] for c in cands]
    assert scores == sorted(scores, reverse=True)
    assert cands[0]["sku"] == "SKU-A003"
    assert cands[0]["reason"]


# ---- 竞品价格监控 ----

def test_competitor_watch_gap_and_drop_alert():
    out = json.loads(competitor_watch.invoke({"query": ""}, config=_staff_cfg()))
    watch = {w["comp_sku"]: w for w in out["watchlist"]}
    assert "COMP-9001" in watch
    # MP-01：竞品 219 vs 我方 199 → 我方更便宜（gap > 0）
    assert watch["COMP-9001"]["gap_pct"] > 0
    alerts = out["alerts"]
    assert alerts, "MP-02 近 7 天降价应触发提醒"
    alert = next(a for a in alerts if a["comp_sku"] == "COMP-9002")
    assert alert["drop_pct"] >= 0.05
    assert "降价" in alert["alert"]


def test_competitor_watch_sku_filter():
    out = json.loads(competitor_watch.invoke(
        {"query": "SKU-A001"}, config=_staff_cfg()))
    assert all(w["sku"] == "SKU-A001" for w in out["watchlist"])
    assert not any(a["comp_sku"] == "COMP-9002" for a in out["alerts"])


# ---- plan_save：两阶段确认 + 幂等 ----

def _plan_cfg(conversation_id: str) -> dict:
    cfg = _staff_cfg("marketing.write")
    cfg["metadata"]["context_id"] = conversation_id
    return cfg


def test_plan_save_two_phase_confirm_and_idempotent():
    cfg = _plan_cfg("ctx-plan-1")
    pending = json.loads(plan_save.invoke(
        {"kind": "ad_adjust", "target": "CP-01", "note": "ROI 达标加投"},
        config=cfg))
    assert pending["status"] == "pending_confirm"
    assert pending["confirm_token"]
    # 未确认不落库
    from shared.db import connect_business_db

    with connect_business_db() as conn:
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM marketing_plans WHERE tenant_id='t_demo'"
        ).fetchone()["n"] == 0

    confirmed = json.loads(plan_save.invoke(
        {"kind": "ad_adjust", "target": "CP-01", "note": "ROI 达标加投",
         "confirm_token": pending["confirm_token"]}, config=cfg))
    assert confirmed["status"] == "confirmed"
    assert confirmed["idempotent"] is False
    with connect_business_db() as conn:
        rows = conn.execute(
            "SELECT plan_id, kind, target, status, biz_key FROM marketing_plans "
            "WHERE tenant_id='t_demo' AND biz_key=?",
            (pending["biz_key"],),
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["status"] == "confirmed"

    # 幂等重放：同会话同 kind+target → 仍只有一张计划单
    replay_pending = json.loads(plan_save.invoke(
        {"kind": "ad_adjust", "target": "CP-01", "note": "重复保存"},
        config=cfg))
    replay = json.loads(plan_save.invoke(
        {"kind": "ad_adjust", "target": "CP-01", "note": "重复保存",
         "confirm_token": replay_pending["confirm_token"]}, config=cfg))
    assert replay["plan_id"] == confirmed["plan_id"]
    assert replay["idempotent"] is True
    with connect_business_db() as conn:
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM marketing_plans WHERE tenant_id='t_demo'"
        ).fetchone()["n"] == 1


def test_plan_save_invalid_confirm_token():
    cfg = _plan_cfg("ctx-plan-2")
    pending = json.loads(plan_save.invoke(
        {"kind": "restock", "target": "SKU-A003"}, config=cfg))
    bad = json.loads(plan_save.invoke(
        {"kind": "restock", "target": "SKU-A003",
         "confirm_token": "not-a-token"}, config=cfg))
    assert bad["error"] == "invalid_confirm_token"
    # 目标不匹配的确认单同样拒绝（scope 绑定校验）
    other = json.loads(plan_save.invoke(
        {"kind": "restock", "target": "SKU-B002",
         "confirm_token": pending["confirm_token"]}, config=cfg))
    assert other["error"] == "invalid_confirm_token"


def test_plan_save_needs_write_scope_and_target():
    read_only = json.loads(plan_save.invoke(
        {"kind": "ad_adjust", "target": "CP-01"}, config=_staff_cfg()))
    assert read_only["error"] == "rejected"
    assert read_only["need_scope"] == "marketing.write"
    no_target = json.loads(plan_save.invoke(
        {"kind": "ad_adjust", "target": ""}, config=_plan_cfg("ctx-plan-3")))
    assert no_target["error"] == "need_target"
