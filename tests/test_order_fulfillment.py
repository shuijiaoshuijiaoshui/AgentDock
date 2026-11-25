"""订单 / 履约域工具合同测试：真实数据 + scope 矩阵 + 写操作两阶段确认。"""

import json

from domains.fulfillment.tools import fulfill_atp, fulfill_track, return_create
from domains.order.tools import order_amend, order_query
from shared.auth import auth_config

CFG_ORDER = auth_config("order.read")
CFG_ORDER["metadata"]["context_id"] = "ctx-1"
CFG_ORDER["metadata"]["customer_id"] = "CU-1001"
CFG_ORDER_W = auth_config("order.write")
CFG_ORDER_W["metadata"]["context_id"] = "ctx-1"
CFG_ORDER_W["metadata"]["customer_id"] = "CU-1001"
CFG_ORDER_W4 = auth_config("order.write")
CFG_ORDER_W4["metadata"]["context_id"] = "ctx-1"
CFG_ORDER_W4["metadata"]["customer_id"] = "CU-1004"
CFG_FULFILL = auth_config("fulfillment.read")
CFG_FULFILL["metadata"]["customer_id"] = "CU-1002"
CFG_BAD = auth_config("merchandising.read")


def _fulfill_cfg(customer_id: str) -> dict:
    cfg = auth_config("fulfillment.read")
    cfg["metadata"]["customer_id"] = customer_id
    return cfg


def _order_cfg(customer_id: str) -> dict:
    cfg = auth_config("order.read")
    cfg["metadata"]["customer_id"] = customer_id
    return cfg


# ---------------------------- 订单域 ----------------------------

def test_order_query_hit():
    out = json.loads(order_query.invoke({"order_no": "SO-2001"}, config=CFG_ORDER))
    assert out["order_no"] == "SO-2001"
    assert out["status_name"] == "已签收"
    assert out["amount"] == 199.0


def test_order_query_by_customer_when_no_order_no():
    """订单号可空：回落按 customer_id 查（客户身份来自 metadata）。"""
    out = json.loads(order_query.invoke({"order_no": ""}, config=CFG_ORDER))
    assert out["customer_id"] == "CU-1001"
    assert out["order_no"] == "SO-2001"


def test_order_query_needs_identifier():
    out = json.loads(
        order_query.invoke({"order_no": ""}, config=auth_config("order.read"))
    )
    assert out["error"] == "customer_id_required"


def test_order_query_not_found():
    out = json.loads(order_query.invoke({"order_no": "SO-9999"}, config=CFG_ORDER))
    assert out["error"] == "not_found"


def test_order_scope_rejected():
    out = json.loads(order_query.invoke({"order_no": "SO-2001"}, config=CFG_BAD))
    assert out["error"] == "rejected"
    assert out["need_scope"] == "order.read"


def test_order_amend_two_phase_and_idempotent():
    """写操作两阶段确认：第一次只预览，带 token 才落库（风险审计 R3）。"""
    preview = json.loads(order_amend.invoke(
        {"order_no": "SO-2004", "address": "深圳市福田区新地址 1 号"},
        config=CFG_ORDER_W4,
    ))
    assert preview["status"] == "pending_confirm"
    token = preview["confirm_token"]
    assert preview["preview"]["order_no"] == "SO-2004"

    # 未带 token 时数据未变
    before = json.loads(order_query.invoke(
        {"order_no": "SO-2004"}, config=_order_cfg("CU-1004")))
    assert before["address"] != "深圳市福田区新地址 1 号"

    done = json.loads(order_amend.invoke(
        {"order_no": "SO-2004", "address": "深圳市福田区新地址 1 号",
         "confirm_token": token},
        config=CFG_ORDER_W4,
    ))
    assert done["status"] == "amended"
    assert done["verified"] is True

    after = json.loads(order_query.invoke(
        {"order_no": "SO-2004"}, config=_order_cfg("CU-1004")))
    assert after["address"] == "深圳市福田区新地址 1 号"


def test_order_amend_bad_token():
    out = json.loads(order_amend.invoke(
        {"order_no": "SO-2004", "address": "随便", "confirm_token": "wrong"},
        config=CFG_ORDER_W4,
    ))
    assert out["error"] == "invalid_confirm_token"


def test_order_amend_rejects_shipped():
    """已发货/已签收订单不可改地址（业务规则前置约束）。"""
    out = json.loads(order_amend.invoke(
        {"order_no": "SO-2001", "address": "改不了", "confirm_token": "x"},
        config=CFG_ORDER_W,
    ))
    assert out["error"] == "not_amendable"
    assert out["status_name"] == "已签收"


# ---------------------------- 履约域 ----------------------------

def test_fulfill_track_nodes():
    out = json.loads(fulfill_track.invoke(
        {"order_no": "SO-2002"}, config=_fulfill_cfg("CU-1002")))
    assert out["carrier"] == "京东物流"
    assert out["status_name"] == "运输中"
    assert len(out["nodes"]) == 3
    assert out["nodes"][0]["description"] == "已揽收"


def test_fulfill_track_no_shipment():
    """待付款订单没有运单：明确说明，不假装有物流。"""
    out = json.loads(fulfill_track.invoke(
        {"order_no": "SO-2004"}, config=_fulfill_cfg("CU-1004")))
    assert out["error"] == "no_shipment"
    assert out["order_status"] == "pending_payment"


def test_fulfill_track_not_found():
    out = json.loads(fulfill_track.invoke(
        {"order_no": "SO-9999"}, config=_fulfill_cfg("CU-1001")))
    assert out["error"] == "not_found"


def test_fulfill_atp_in_stock():
    out = json.loads(fulfill_atp.invoke({"sku": "SKU-A001"}, config=CFG_FULFILL))
    assert out["available"] == 320
    assert out["ats_days"] == 1


def test_fulfill_atp_off_shelf():
    out = json.loads(fulfill_atp.invoke({"sku": "SKU-B001"}, config=CFG_FULFILL))
    assert out["available"] == 0
    assert out["on_sale"] is False


def test_fulfill_scope_rejected():
    out = json.loads(fulfill_track.invoke({"order_no": "SO-2002"}, config=CFG_BAD))
    assert out["error"] == "rejected"
    assert out["need_scope"] == "fulfillment.read"


# ---------------------------- 履约域 · 退库单（金额操作转人工：Agent 只建草稿） ----------------------------

def _fulfill_w(customer_id: str) -> dict:
    cfg = auth_config("fulfillment.write")
    cfg["metadata"]["customer_id"] = customer_id
    cfg["metadata"]["context_id"] = "ctx-return"
    return cfg


def test_return_create_pending_confirm():
    """Agent 只创建退库单草稿（pending_confirm），入库确认归仓库人工。"""
    out = json.loads(return_create.invoke(
        {"order_no": "SO-2001", "reason": "质量问题退货"},
        config=_fulfill_w("CU-1001"),
    ))
    assert out["status"] == "pending_confirm"
    assert out["order_no"] == "SO-2001"
    assert out["idempotent"] is False
    assert "人工" in out["hint"]


def test_return_create_idempotent_same_context():
    cfg = _fulfill_w("CU-1001")
    first = json.loads(return_create.invoke(
        {"order_no": "SO-2001", "reason": "质量问题退货"}, config=cfg))
    again = json.loads(return_create.invoke(
        {"order_no": "SO-2001", "reason": "质量问题退货"}, config=cfg))
    assert again["return_id"] == first["return_id"]
    assert again["idempotent"] is True


def test_return_create_scope_rejected():
    out = json.loads(return_create.invoke(
        {"order_no": "SO-2001", "reason": "r"}, config=_fulfill_cfg("CU-1001")))
    assert out["error"] == "rejected"


def test_return_create_order_not_found_or_not_owned():
    out = json.loads(return_create.invoke(
        {"order_no": "SO-9999", "reason": "r"}, config=_fulfill_w("CU-1001")))
    assert out["error"] == "not_found"


def test_return_create_needs_order_no():
    out = json.loads(return_create.invoke(
        {"order_no": "", "reason": ""}, config=_fulfill_w("CU-1001")))
    assert out["error"] == "need_order_no"
