"""多轮历史注入（M4 / PRD 10.4）：近 N 轮原文 + 滚动摘要。

单测：fake 槽位继承（指代词）、显式 id 优先、历史块 PII 脱敏、
摘要生成的确定性阈值行为。
e2e（复用 test_customer_e2e 四进程栈，fake LLM）：>6 轮会话触发滚动摘要落库、
跨轮指代（"它多少钱"解析到上一轮 SKU）、交接包带真实近 6 轮历史。
"""

import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("ENV", "test")
os.environ.setdefault("AUTH_SECRET", "test-secret")

from orchestrator.graph import (  # noqa: E402
    _history_block,
    _maybe_session_summary,
    rule_understand,
)
from shared.llm_guard import assert_no_pii  # noqa: E402

# 复用四进程栈 fixture（module 作用域）：必须模块级导入 pytest 才可见
from tests.test_customer_e2e import stack  # noqa: E402,F401


HIST = {
    "summary": "",
    "upto_message_id": 0,
    "recent_turns": [
        {"id": 1, "role": "customer", "text": "SKU-A002 到手价多少"},
        {"id": 2, "role": "ai", "text": "【商品】SKU-A002 估算到手价 141.55 元"},
        {"id": 3, "role": "customer", "text": "那 SO-2001 发货了吗"},
        {"id": 4, "role": "ai", "text": "【履约】SO-2001 已发货"},
    ],
}


def test_rule_understand_pronoun_carries_sku():
    u = rule_understand("它多少钱", HIST)
    assert u["intent"] == "promo_evaluate"
    assert u["slots"]["sku"] == "SKU-A002"


def test_rule_understand_without_history_keeps_empty_slot():
    u = rule_understand("它多少钱", {})
    assert u["intent"] == "promo_evaluate"
    assert not u["slots"].get("sku")


def test_rule_understand_explicit_id_wins():
    u = rule_understand("SKU-B001 多少钱", HIST)
    assert u["intent"] == "promo_evaluate"
    assert u["slots"]["sku"] == "SKU-B001"


def test_rule_understand_pronoun_carries_order_no():
    u = rule_understand("它发货了吗", HIST[-2:] if isinstance(HIST, list) else {
        **HIST, "recent_turns": HIST["recent_turns"][2:],
    })
    assert u["intent"] == "fulfill_track"
    assert u["slots"]["order_no"] == "SO-2001"


def test_history_block_masks_pii():
    hist = {"summary": "", "upto_message_id": 0, "recent_turns": [
        {"id": 1, "role": "customer",
         "text": "我手机 13813813800，SKU-A002 能退吗"},
    ]}
    block = _history_block(hist)
    assert "SKU-A002" in block
    assert "13813813800" not in block  # 原始手机号不得出现在进 LLM 的历史块里
    assert_no_pii(block)


def test_session_summary_deterministic_and_threshold():
    state = {
        "history": {
            "summary": "旧摘要：客户问过 SKU-A002。",
            "upto_message_id": 2,
            "carry_turns": [
                {"id": 5, "role": "customer", "text": "SO-2001 发货了吗"},
                {"id": 6, "role": "ai", "text": "已发货"},
            ],
        },
    }
    s1 = _maybe_session_summary(state, None)
    s2 = _maybe_session_summary(state, None)
    assert s1 == s2  # fake 模式确定性
    assert "旧摘要" in s1["summary"]
    assert "SO-2001" in s1["summary"]
    assert s1["upto_message_id"] == 6


def test_session_summary_skips_when_no_carry():
    assert _maybe_session_summary({"history": {
        "summary": "", "upto_message_id": 6, "carry_turns": [],
    }}, None) == {}


# ---------------------------------------------------------------------------
# e2e：>6 轮 → 摘要落库；跨轮指代解析；交接包带真实近 6 轮
# ---------------------------------------------------------------------------

def _db_one(stack, sql: str, args: tuple = ()):  # noqa: ANN001
    conn = sqlite3.connect(stack["console_db"])
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, args).fetchone()
    finally:
        conn.close()


def test_multi_turn_summary_and_pronoun_e2e(stack):
    from tests.test_customer_e2e import _login, _send

    customer = _login(stack, "customer")
    sent = _send(stack, customer, "SKU-A002 到手价多少")
    cid = sent["conversation_id"]
    # 续同会话凑满 >6 轮（6 条消息 = 3 对；加上首问共 4 个客户轮次）
    for text in ("你好", "七天无理由退货政策是什么", "帮我看下 SO-2001 发货了吗",
                 "对账单 2026-01 查一下", "我的客户资料"):
        r = _send(stack, customer, text, cid=cid)
        assert r["type"] in ("ai", "clarify"), r

    # 下一轮摘要生成条件已满足（首问掉出最近窗口）→ 本轮回写滚动摘要
    r = _send(stack, customer, "它多少钱", cid=cid)
    assert r["type"] == "ai"
    row = _db_one(stack, "SELECT * FROM session_summaries WHERE conversation_id=?",
                  (cid,))
    assert row is not None, "滚动摘要应已落库"
    assert "SKU-A002" in row["summary"]  # 摘要覆盖了掉窗的首问
    assert int(row["upto_message_id"]) >= 2
    # 跨轮指代：本轮 promo_evaluate 通过继承拿到上一轮的 SKU
    assert "SKU-A002" in str(r.get("reply") or ""), r


def test_handoff_package_carries_real_recent_turns(stack):
    from tests.test_customer_e2e import _login, _send

    customer = _login(stack, "customer")
    sent = _send(stack, customer, "你好")
    cid = sent["conversation_id"]
    _send(stack, customer, "SKU-A001 多少钱", cid=cid)
    _send(stack, customer, "七天无理由退货政策是什么", cid=cid)
    _send(stack, customer, "转人工", cid=cid)

    row = _db_one(stack, "SELECT package FROM handoff_queue WHERE conversation_id=?",
                  (cid,))
    assert row is not None
    package = json.loads(row["package"])
    assert len(package["history"]) == 6  # 渠道层真实近 6 轮（旧实现只有当轮 1 条）
    assert any("SKU-A001" in h for h in package["history"])
