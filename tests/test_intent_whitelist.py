"""澄清选项白名单化（防止把模型自造标签展示给用户）。

- _clarify_options：只保留 _INTENT_SET 内且非 unknown 的标签，去重保序、最多两个；
- understand：过滤后不足两个选项 → 不澄清、直接落单（route 查表为 None → nomatch 拒答）。
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import orchestrator.graph as graph  # noqa: E402


class _Interrupted(Exception):
    """测试用哨兵：模拟 interrupt() 挂起。"""


def test_clarify_options_drops_wild_labels():
    assert graph._clarify_options(["product_query", "refund_query"]) == []
    assert graph._clarify_options(["PRODUCT_LOOKUP", "product-lookup"]) == []
    assert graph._clarify_options(["查商品价格"]) == []


def test_clarify_options_keeps_legal_dedupes_and_caps():
    assert graph._clarify_options(
        ["product_lookup", "PRODUCT_LOOKUP", "product_lookup", "promo_evaluate"]
    ) == ["product_lookup", "promo_evaluate"]
    assert graph._clarify_options(
        ["greet", "product_lookup", "refund_status"]
    ) == ["greet", "product_lookup"]  # 最多两个


def test_clarify_options_excludes_unknown_and_non_strings():
    assert graph._clarify_options(["unknown", "refund_query"]) == []
    assert graph._clarify_options([None, 3, "greet", "unknown"]) == ["greet"]
    assert graph._clarify_options(None) == []


def test_understand_skips_clarify_when_options_insufficient(monkeypatch):
    """野标签占满 top2 → 过滤后 0 个合法选项 → 不澄清，直接返回 unknown（进 nomatch）。"""
    monkeypatch.setattr(graph, "_classify", lambda *a, **k: {
        "intent": "unknown", "slots": {}, "confidence": 0.2,
        "top2": ["product_query", "refund_query"],
    })
    monkeypatch.setattr(graph, "interrupt", lambda payload: pytest.fail("不该澄清"))

    out = graph.understand(
        {"user_text": "随便问问", "clarify_count": 0, "risk_flag": False,
         "history": {}},
        None,
    )
    assert out["intent"] == "unknown"
    assert out["confidence"] == 0.2
    assert "clarify_count" not in out  # 没走澄清分支


def test_understand_clarifies_with_filtered_options(monkeypatch):
    """合法选项 >=2 → 正常澄清；问题里只出现白名单标签。"""
    monkeypatch.setattr(graph, "_classify", lambda *a, **k: {
        "intent": "unknown", "slots": {}, "confidence": 0.2,
        "top2": ["product_lookup", "promo_evaluate"],
    })
    captured = {}

    def _fake_interrupt(payload):
        captured.update(payload)
        raise _Interrupted

    monkeypatch.setattr(graph, "interrupt", _fake_interrupt)
    with pytest.raises(_Interrupted):
        graph.understand(
            {"user_text": "那个东西多少钱", "clarify_count": 0,
             "risk_flag": False, "history": {}},
            None,
        )
    assert captured["options"] == ["product_lookup", "promo_evaluate"]
    assert "product_lookup" in captured["question"]
    assert "promo_evaluate" in captured["question"]
