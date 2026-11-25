"""路由表确定性测试（AC-21：同输入 100 次必须走同一条域链）。"""

from orchestrator.route_table import route_plan


def test_determinism_100_runs():
    """AC-21：同一句话（同 intent+槽位+置信）100 次路由结果完全一致。"""
    expected = route_plan("product_lookup", {"sku": "SKU-A001"}, 0.9)
    assert expected is not None
    for _ in range(100):
        assert route_plan("product_lookup", {"sku": "SKU-A001"}, 0.9) == expected


def test_unknown_intent_goes_nomatch():
    # M2 放行 order_query/fulfill_track；M3 接入 refund_status/cancel_refund/
    # order_amend 串行链与 promo_arbitrate 仲裁链；未收录意图仍走 nomatch
    assert route_plan("order_cancel_and_refund", {}, 0.9) is None  # 未收录
    assert route_plan("invoice_void", {}, 0.9) is None
    assert route_plan("unknown", {}, 0.3) is None


def test_arbitrate_mode_with_deps():
    """M3 仲裁链：分层 DAG——商品域估算依赖订单域的 sku（PRD 7.6/3.4）。"""
    plan = route_plan("promo_arbitrate", {}, 0.9)
    assert plan["mode"] == "arbitrate"
    assert plan["domains"] == ["order", "merchandising", "finance"]
    assert plan["deps"] == {"merchandising": ["order"]}


def test_serial_chains():
    """M3 串行链：列表即链序（PRD 7.9 售后→履约→财务；7.7 订单→财务）。"""
    plan = route_plan("refund_status", {}, 0.9)
    assert plan["mode"] == "serial"
    assert plan["domains"] == ["aftersale", "fulfillment", "finance"]

    plan = route_plan("cancel_refund", {}, 0.9)
    assert plan["mode"] == "serial"
    assert plan["domains"] == ["order", "finance"]

    plan = route_plan("order_amend", {}, 0.9)
    assert plan["mode"] == "serial"
    assert plan["domains"] == ["fulfillment", "order"]


def test_low_confidence_goes_nomatch():
    """低置信不放行（澄清失败兜底走拒答）。"""
    assert route_plan("product_lookup", {"sku": "SKU-A001"}, 0.5) is None


def test_smalltalk_mode():
    plan = route_plan("greet", {}, 0.95)
    assert plan is not None
    assert plan["mode"] == "smalltalk"
    assert plan["domains"] == []


def test_merchandising_intents_route_to_single_domain():
    for intent in ("product_lookup", "product_search", "product_compare", "promo_evaluate"):
        plan = route_plan(intent, {}, 0.9)
        assert plan is not None, intent
        assert plan["domains"] == ["merchandising"]
        assert plan["mode"] == "single"


def test_subsidy_check_routes_to_merchandising():
    """国补资格：单域（商品）路由——价格差异只来自活动/国补。"""
    plan = route_plan("subsidy_check", {"sku": "SKU-A001"}, 0.9)
    assert plan is not None
    assert plan["domains"] == ["merchandising"]
    assert plan["mode"] == "single"


def test_marketing_intents_route_to_single_domain():
    """营销经营分析（toB marketing 域）：四个分析意图单域路由。"""
    for intent in ("ad_analysis", "product_selection", "competitor_watch",
                   "ad_plan_save"):
        plan = route_plan(intent, {}, 0.9)
        assert plan is not None, intent
        assert plan["domains"] == ["marketing"]
        assert plan["mode"] == "single"


def test_hot_restock_arbitrate_with_deps():
    """爆款→备货仲裁链：marketing(爆款增速) → fulfillment(可承诺库存)，
    依赖边与 ARBITRATE_INJECTIONS 配套（top_sku 注入）。"""
    plan = route_plan("hot_restock", {}, 0.9)
    assert plan is not None
    assert plan["mode"] == "arbitrate"
    assert plan["domains"] == ["marketing", "fulfillment"]
    assert plan["deps"] == {"fulfillment": ["marketing"]}
    expected = route_plan("hot_restock", {"query": "近7天"}, 0.9)
    for _ in range(100):
        assert route_plan("hot_restock", {"query": "近7天"}, 0.9) == expected
