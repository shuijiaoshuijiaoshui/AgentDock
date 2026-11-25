"""编排层纯函数测试：权威源裁决（PRD 7.6 / AC-22）、上游否决、快照漂移（7.8）。"""

from datetime import datetime, timedelta, timezone

from orchestrator.arbitrate import (
    arbitrate_fields,
    arbitrate_value,
    check_snapshot_drift,
    ready_domains,
    veto_reason,
)


# ---------------------------- 权威源裁决（7.6） ----------------------------

def test_arbitrate_drops_non_authority_fields():
    """非权威域携带权威字段 → 丢弃该字段，其余数据保留。"""
    artifacts = {
        "aftersale": {"authority_domain": "aftersale",
                      "data": {"eligible": True, "refund_amount": 199.0}},
        "finance": {"authority_domain": "finance",
                    "data": {"refund_amount": 159.0}},
    }
    cleaned, log = arbitrate_fields(artifacts)
    # 售后的估算金额被丢弃（退款金额权威=财务，3.4）
    assert "refund_amount" not in cleaned["aftersale"]["data"]
    assert cleaned["aftersale"]["data"]["eligible"] is True  # 其余字段不动
    assert cleaned["finance"]["data"]["refund_amount"] == 159.0  # 权威值保留
    dropped_event = next(e for e in log if e.get("event") == "dropped_non_authority_fields")
    assert dropped_event["domain"] == "aftersale"
    assert dropped_event["dropped"] == {"refund_amount": 199.0}  # 候选值留档


def test_arbitrate_authority_field_untouched():
    """权威域自己的字段不受影响。"""
    artifacts = {"finance": {"authority_domain": "finance",
                             "data": {"arrival_time": "3 个工作日"}}}
    cleaned, log = arbitrate_fields(artifacts)
    assert cleaned["finance"]["data"]["arrival_time"] == "3 个工作日"
    assert log[0]["event"] == "no_conflict"


def test_arbitrate_pure_function_replayable():
    """裁决是纯函数：同输入同输出（可回放，AC-22 的可测性基础）。"""
    artifacts = {"fulfillment": {"authority_domain": "fulfillment",
                                 "data": {"atp": 5}}}
    c1, l1 = arbitrate_fields(artifacts)
    c2, l2 = arbitrate_fields(artifacts)
    assert c1 == c2 and l1 == l2


# ---------------------------- 上游否决（7.9 第 4 步） ----------------------------

REFUND_CHAIN = ["aftersale", "fulfillment", "finance"]
AMEND_CHAIN = ["fulfillment", "order"]


def test_veto_on_ineligible():
    """资格不通过 → 否决下游（PRD 7.9：不符合 → 取消下游并直接作答）。"""
    artifacts = {"aftersale": {"data": {"eligible": False, "conclusion": "保修外"}}}
    reason = veto_reason("refund_status", REFUND_CHAIN, artifacts)
    assert reason is not None and "资格判定不通过" in reason


def test_veto_on_upstream_error():
    """上游系统失败 → 否决下游（7.8：资格判定域失败 → 整体拒答）。"""
    artifacts = {"aftersale": {"data": {"error": "unreachable: ConnectError"}}}
    assert veto_reason("refund_status", REFUND_CHAIN, artifacts) is not None


def test_no_veto_when_eligible():
    artifacts = {"aftersale": {"data": {"eligible": True}}}
    assert veto_reason("refund_status", REFUND_CHAIN, artifacts) is None


def test_veto_amend_when_shipped():
    """已发货订单不可改地址；无运单（未发货）放行。"""
    shipped = {"fulfillment": {"data": {"status": "delivered", "status_name": "已签收"}}}
    reason = veto_reason("order_amend", AMEND_CHAIN, shipped)
    assert reason is not None and "已发货" in reason

    unshipped = {"fulfillment": {"data": {"error": "no_shipment",
                                          "order_status": "pending_payment"}}}
    assert veto_reason("order_amend", AMEND_CHAIN, unshipped) is None


def test_tail_error_does_not_veto():
    """尾跳失败不是"否决"（没有下游可取消），走局部交付/Saga 路径。"""
    artifacts = {"aftersale": {"data": {"eligible": True}},
                 "fulfillment": {"data": {"status": "in_transit"}},
                 "finance": {"data": {"error": "no_refund"}}}
    assert veto_reason("refund_status", REFUND_CHAIN, artifacts) is None


# ---------------------------- 快照漂移（7.8） ----------------------------

def _ts(seconds_ago: int) -> str:
    return datetime.now(timezone.utc).isoformat()


def test_snapshot_drift_within_limit():
    now = datetime.now(timezone.utc)
    artifacts = {
        "a": {"as_of": now.isoformat()},
        "b": {"as_of": (now - timedelta(seconds=60)).isoformat()},
    }
    assert check_snapshot_drift(artifacts) is None


def test_snapshot_drift_over_limit():
    """超 5 分钟 → 明确标注，不得静默混用（7.8）。"""
    now = datetime.now(timezone.utc)
    artifacts = {
        "order": {"as_of": now.isoformat()},
        "fulfillment": {"as_of": (now - timedelta(seconds=600)).isoformat()},
    }
    note = check_snapshot_drift(artifacts)
    assert note is not None and "时点不一致" in note and "order" in note


def test_snapshot_single_artifact_no_check():
    assert check_snapshot_drift({"a": {"as_of": _ts(0)}}) is None
    assert check_snapshot_drift({}) is None


# ---------------------------- 值裁决（PRD 7.6 标准签名） ----------------------------

def test_arbitrate_value_authority_wins():
    """实付金额：财务权威赢，营销估算被丢弃且留档。"""
    r = arbitrate_value("deal_amount", [
        {"domain": "merchandising", "value": 169.0, "estimate": True},
        {"domain": "finance", "value": 199.0},
    ])
    assert r["value"] == 199.0 and r["authority"] == "finance"
    assert r["discarded"][0]["domain"] == "merchandising"


def test_arbitrate_value_no_substitution():
    """权威域无数据 → 不得用非权威域补位（7.6 规则 2），value=None。"""
    r = arbitrate_value("deal_amount", [
        {"domain": "merchandising", "value": 169.0, "estimate": True},
    ])
    assert r["value"] is None
    assert r["discarded"] == []
    assert "补位" in r["reason"]


# ---------------------------- 分层 DAG 调度（纯函数部分） ----------------------------

def test_ready_domains_deps_gating():
    """有依赖的域在上游完成前不发。"""
    domains = ["order", "merchandising", "finance"]
    deps = {"merchandising": ["order"]}
    ready, cancelled = ready_domains(domains, deps, {})
    assert set(ready) == {"order", "finance"}   # 无依赖域并发
    assert cancelled == []


def test_ready_domains_fires_after_upstream():
    artifacts = {"order": {"data": {"sku": "SKU-A001", "amount": 199.0}}}
    ready, _ = ready_domains(["order", "merchandising", "finance"],
                             {"merchandising": ["order"]}, artifacts)
    assert set(ready) == {"merchandising", "finance"}


def test_ready_domains_cancels_on_upstream_failure():
    """上游失败 → 依赖域取消（不调用）；无依赖域照发。"""
    artifacts = {"order": {"data": {"error": "not_found"}}}
    ready, cancelled = ready_domains(["order", "merchandising", "finance"],
                                     {"merchandising": ["order"]}, artifacts)
    assert ready == ["finance"]
    assert cancelled == [("merchandising", "上游 order 失败")]


def test_ready_domains_all_done():
    artifacts = {d: {"data": {}} for d in ("order", "merchandising", "finance")}
    ready, cancelled = ready_domains(list(artifacts), {}, artifacts)
    assert ready == [] and cancelled == []
