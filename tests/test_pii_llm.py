"""PII 不进 LLM：合成/域 ReAct 前字段级脱敏。"""

import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, ToolMessage

import orchestrator.graph as og
from shared import security_db
from shared.auth import auth_config
from shared.llm_guard import (
    assert_no_pii,
    sanitize_for_llm,
    sanitize_messages_for_llm,
    sanitize_text,
)


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setenv("SHARED_DB_PATH", str(tmp_path / "shared.db"))
    security_db.reset_for_tests(str(tmp_path / "shared.db"))
    yield
    security_db.reset_for_tests()


class _Recorder:
    def __init__(self) -> None:
        self.payload = ""

    def invoke(self, messages, config=None):  # noqa: ANN001, ANN002
        self.payload = str(messages[-1].content)
        return AIMessage(content="已处理")


def test_synthesize_sanitizes_artifacts_before_llm(monkeypatch):
    recorder = _Recorder()
    monkeypatch.setattr(
        og, "get_settings",
        lambda: SimpleNamespace(llm_provider="openai", llm_main_model="test-model"),
    )
    import shared.llm as llm_mod

    monkeypatch.setattr(llm_mod, "get_main_llm", lambda: recorder)
    cfg = auth_config("order.read", role="service", actor_id="svc")
    cfg["metadata"].update({"context_id": "c1", "trace_id": "t1"})
    state = {
        "user_text": "客户手机 13800001234 问订单",
        "intent": "order_query",
        "artifacts": {
            "order": {
                "authority_domain": "order",
                "as_of": "2026-10-27T00:00:00+00:00",
                "data": {
                    "order_no": "SO-2001",
                    "customer_id": "CU-1001",
                    "address": "杭州市西湖区文三路 100 号",
                    "phone": "13800001234",
                    "amount": 199.0,
                },
            },
        },
        "decision_log": [],
        "saga_result": {},
        "snapshot_id": "s1",
        "context_id": "c1",
        "trace_id": "t1",
    }
    out = og.synthesize(state, cfg)
    assert out["final_text"] == "已处理"
    payload = json.loads(recorder.payload)
    assert_no_pii(payload)
    assert "SO-2001" in recorder.payload and "199.0" in recorder.payload
    assert "杭州市西湖区" not in recorder.payload
    assert "13800001234" not in recorder.payload
    assert "CU-1001" not in recorder.payload


def test_fake_summary_fallback_never_dumps_raw_data():
    """无域摘要时给通用答复，禁止把内部 data 原样回给客户。"""
    artifacts = {
        "order": {
            "authority_domain": "order",
            "as_of": "2026-10-27T00:00:00+00:00",
            "data": {
                "order_no": "SO-2001",
                "tenant_id": "t_demo",
                "customer_id": "CU-1001",
                "tool_events": [{"tool": "order_query"}],
            },
        }
    }
    text = "；".join(og._fake_artifact_parts(artifacts))
    assert "已完成查询" in text and "结果卡片" in text
    for internal in ("SO-2001", "tenant_id", "customer_id", "tool_events", "{"):
        assert internal not in text


def test_fake_summary_prefers_domain_summary():
    artifacts = {
        "order": {
            "authority_domain": "order",
            "as_of": "2026-10-27T00:00:00+00:00",
            "summary": "订单 SO-2001 当前状态：已签收。",
            "data": {"tenant_id": "t_demo", "order_no": "SO-2001"},
        }
    }
    text = "；".join(og._fake_artifact_parts(artifacts))
    assert "订单 SO-2001 当前状态：已签收。" in text
    assert "tenant_id" not in text


def test_domain_tool_messages_are_sanitized():
    tool_msg = ToolMessage(
        content=json.dumps({
            "order_no": "SO-2001",
            "address": "杭州市西湖区文三路 100 号",
            "phone": "13800001234",
            "email": "a@b.com",
            "customer_id": "CU-1001",
        }, ensure_ascii=False),
        tool_call_id="call-1",
        name="order_query",
    )
    cleaned = sanitize_messages_for_llm([tool_msg])[0]
    assert "杭州市西湖区" not in cleaned.content
    assert "13800001234" not in cleaned.content
    assert "a@b.com" not in cleaned.content
    assert "CU-1001" not in cleaned.content
    assert "SO-2001" in cleaned.content


def test_sanitize_for_llm_nested_and_regex():
    value = {
        "order": {
            "order_no": "SO-2001",
            "address": "杭州市",
            "customer_id": "CU-1001",
            "note": "联系 13800001234",
            "items": [{"receiver": "王小明", "sku": "SKU-A001"}],
        }
    }
    cleaned = sanitize_for_llm(value)
    assert cleaned["order"]["address"] == "[REDACTED]"
    assert cleaned["order"]["customer_id"] == "[REDACTED]"
    assert "13800001234" not in cleaned["order"]["note"]
    assert cleaned["order"]["items"][0]["receiver"] == "[REDACTED]"
    assert cleaned["order"]["items"][0]["sku"] == "SKU-A001"
    assert sanitize_text('{"order_no":"SO-2001","phone":"13800001234"}').count(
        "13800001234"
    ) == 0
