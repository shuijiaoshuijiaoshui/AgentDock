"""人机协同单测：转人工触发条件、交接包、坐席专属工具、队列存储。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from console import db
from domains.customer.tools import customer_search
from orchestrator.graph import _RISK_RE, after_nomatch, after_route, rule_understand
from shared.auth import auth_config

# 种子 Mock 数据（customers 表）
import importlib.util

spec = importlib.util.spec_from_file_location(
    "seed_mock", Path(__file__).resolve().parents[1] / "scripts" / "seed_mock.py"
)
_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(_mod)
_mod.seed()


# ---- 触发条件 ----

def test_request_human_intent():
    for text in ("帮我转人工", "找人工客服", "转人工服务"):
        u = rule_understand(text)
        assert u["intent"] == "request_human", text


def test_risk_words():
    assert _RISK_RE.search("我要投诉你们")
    assert _RISK_RE.search("再不解决就找315")
    assert not _RISK_RE.search("查询商品详情")


def test_after_route_handoff_priority():
    base = {"plan": None, "intent": "unknown"}
    assert after_route({**base, "intent": "request_human"}) == "handoff"
    assert after_route({**base, "risk_flag": True}) == "handoff"
    assert after_route({**base, "hit_wall_count": 2}) == "handoff"
    assert after_route({**base, "hit_wall_count": 1}) == "nomatch"
    plan = {"mode": "single", "domains": ["merchandising"]}
    assert after_route({"plan": plan, "intent": "product_lookup", "risk_flag": False,
                        "hit_wall_count": 0}) == "advance"  # M2 起 dispatch 更名 advance


def test_after_nomatch_second_wall_goes_handoff():
    assert after_nomatch({"hit_wall_count": 2}) == "handoff"
    assert after_nomatch({"hit_wall_count": 1}) == "deliver"


# ---- 坐席专属工具：查用户信息（已升格为 customer 域 customer.search 工具） ----

def test_customer_query_by_id():
    import json

    rows = json.loads(customer_search.invoke(
        {"query": "CU-1001"}, config=auth_config("customer.read")))
    assert rows[0]["name"] == "王小明"


def test_customer_query_by_name_and_phone_tail():
    import json

    cfg = auth_config("customer.read")
    assert json.loads(customer_search.invoke(
        {"query": "李商户"}, config=cfg))[0]["tier"] == "银牌"
    assert json.loads(customer_search.invoke(
        {"query": "9012"}, config=cfg))[0]["customer_id"] == "CU-1003"


def test_customer_query_miss():
    import json

    assert json.loads(customer_search.invoke(
        {"query": "不存在的人"}, config=auth_config("customer.read"))) == []


# ---- 队列存储 ----

@pytest.fixture()
def tmp_db(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "console.db"))
    monkeypatch.setattr(db, "_conn", None)
    return tmp_path


def test_handoff_queue_lifecycle(tmp_db):
    conv = db.create_conversation()
    cid = conv["id"]
    pkg = {"context_id": cid, "trace_id": "t1", "reasons": ["用户明确要求转人工"],
           "ai_draft": "", "user_text": "转人工"}
    db.enqueue_handoff(pkg)

    h = db.get_handoff(cid)
    assert h["status"] == "pending"
    assert "转人工" in h["package"]["user_text"]

    assert db.take_handoff(cid) is True
    assert db.take_handoff(cid) is False  # 二次接管拒绝
    assert db.get_handoff(cid)["status"] == "taken"


# ---- 国补意图（价格差异只来自活动/国补） ----

def test_subsidy_intent_rule():
    assert rule_understand("这个显示器有国补吗")["intent"] == "subsidy_check"
    assert rule_understand("能不能以旧换新")["intent"] == "subsidy_check"
    # 不抢促销价意图
    assert rule_understand("这个鼠标到手价多少")["intent"] == "promo_evaluate"


# ---- 营销经营分析意图（fake 规则，toB 商家服务端） ----

def test_marketing_intents_rule_understand():
    assert rule_understand("看下最近14天的投流效果")["intent"] == "ad_analysis"
    assert rule_understand("ROI 怎么样")["intent"] == "ad_analysis"
    assert rule_understand("帮我保存方案")["intent"] == "ad_plan_save"
    assert rule_understand("啥卖得最火")["intent"] == "hot_restock"
    assert rule_understand("竞品降价了吗")["intent"] == "competitor_watch"
    assert rule_understand("帮我选品")["intent"] == "product_selection"


def test_marketing_rules_do_not_shadow_legacy_intents():
    # "降价"曾命中改价分支：营销分支先匹配竞品语境，但订单改价仍保留
    assert rule_understand("订单 SO-2001 改价到 100")["intent"] == "order_price_amend"
    # 无爆款语境的补货仍走可承诺库存
    assert rule_understand("SKU-A003 补货吗")["intent"] == "fulfill_atp"
    # 投诉/转人工优先级不受影响
    assert rule_understand("我要投诉")["intent"] != "ad_analysis"
