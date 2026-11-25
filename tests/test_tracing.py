"""跨进程 trace（PRD 7.10 / M4）。

单测：traceparent 往返、span 树重建、关闭时 no-op、采样比 0 静默丢弃、
FileSpanExporter 落盘行。
e2e（复用 test_customer_e2e 四进程栈，tracing 已开）：一次客户消息跨
console/orchestrator/customer/securityd 四进程，span 重建成以 console 为根的
调用树，且 OTel trace_id 与审计/agent_runs 的 trace_id 同号。
"""

import json
import os
import sys
import uuid
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("ENV", "test")
os.environ.setdefault("AUTH_SECRET", "test-secret")

from shared.tracing import (  # noqa: E402
    build_span_tree,
    build_traceparent,
    current_traceparent,
    inject_trace_headers,
    new_trace_id,
    parse_traceparent,
    read_trace_spans,
    start_server_span,
)
# 复用四进程栈 fixture（module 作用域）：必须模块级导入 pytest 才可见
from tests.test_customer_e2e import _login, _send, stack  # noqa: E402,F401


def _st(monkeypatch, tmp_path, **kw):  # noqa: ANN001, ANN003
    from types import SimpleNamespace

    import shared.tracing as t

    defaults = dict(
        env="test", tracing_enabled=True, tracing_sample_ratio=1.0,
        trace_dir=str(tmp_path), service_name="ut",
    )
    defaults.update(kw)
    st = SimpleNamespace(**defaults)
    monkeypatch.setattr(t, "get_settings", lambda: st)
    return t


def test_traceparent_roundtrip():
    tid, sid = new_trace_id(), uuid.uuid4().hex[:16]
    tp = build_traceparent(tid, sid)
    assert parse_traceparent(tp) == (tid, sid)
    assert parse_traceparent("00-short-nope-01") is None
    assert parse_traceparent("garbage") is None


def test_disabled_is_noop(monkeypatch, tmp_path):
    from types import SimpleNamespace

    import shared.tracing as t

    monkeypatch.setattr(t, "get_settings", lambda: SimpleNamespace(
        env="test", tracing_enabled=False, tracing_sample_ratio=1.0,
        trace_dir=str(tmp_path), service_name="ut",
    ))
    t._TRACER = None  # 与其他用例隔离
    assert t.current_traceparent() == ""
    assert t.inject_trace_headers({"a": "b"}) == {"a": "b"}
    with t.start_server_span("x") as span:
        assert span is None
    assert not (tmp_path / "ut.jsonl").exists()
    t._TRACER = None


def test_span_export_and_tree(monkeypatch, tmp_path):
    t = _st(monkeypatch, tmp_path, service_name="svc-a")
    os.environ["SERVICE_NAME"] = "svc-a"
    t._TRACER = None
    tid = new_trace_id()
    with t.start_server_span("root", trace_id=tid):
        assert t.current_traceparent().startswith(f"00-{tid}-")
        with t.start_server_span("child", parent_traceparent=t.current_traceparent()):
            pass
    t._TRACER = None  # SimpleSpanProcessor 同步落盘，收尾即可重置

    spans = read_trace_spans(tid, trace_dir=str(tmp_path))
    assert {s["service"] for s in spans} == {"svc-a"}
    tree = build_span_tree(spans)
    assert tree["name"] == "root"
    assert tree["trace_id"] == tid
    assert [c["name"] for c in tree["children"]] == ["child"]
    assert tree["children"][0]["parent_span_id"] == tree["span_id"]
    os.environ.pop("SERVICE_NAME", None)
    t._TRACER = None


def test_sample_ratio_zero_drops_everything(monkeypatch, tmp_path):
    t = _st(monkeypatch, tmp_path, service_name="svc-z", tracing_sample_ratio=0.0)
    t._TRACER = None
    with t.start_server_span("root", trace_id=new_trace_id()) as span:
        # 头部采样按 trace_id 确定性丢弃：得到 NonRecordingSpan（非 None）
        assert span is not None and not span.is_recording()
    assert t.current_traceparent() == ""
    t._TRACER = None


def test_exporter_row_shape(monkeypatch, tmp_path):
    t = _st(monkeypatch, tmp_path, service_name="svc-r")
    t._TRACER = None
    with t.start_server_span("row", attributes={"k": "v", "bad": object()}):
        pass
    t._TRACER = None
    rows = [json.loads(l) for l in
            (tmp_path / "svc-r.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    row = rows[0]
    assert row["name"] == "row" and row["status"] in ("OK", "UNSET")
    assert row["attributes"]["k"] == "v"
    # OTel 对非法属性类型先置 None（有 warning），_jsonable 原样透传
    assert row["attributes"]["bad"] is None
    assert parse_traceparent(build_traceparent(row["trace_id"], row["span_id"]))


# ---------------------------------------------------------------------------
# e2e：四进程栈，一次客户消息 → console→orchestrator→customer→securityd
# ---------------------------------------------------------------------------

async def test_cross_process_span_tree(stack):
    from tests.test_customer_e2e import _login, _send

    customer = _login(stack, "customer")
    sent = _send(stack, customer, "我的授信额度还有多少")
    cid = sent["conversation_id"]
    assert sent["type"] == "ai"

    # agent_runs 里这一轮的 trace_id（与审计同号）
    r = httpx.get(
        f"http://127.0.0.1:{stack['ports']['console']}"
        f"/api/conversations/{cid}/trace",
        headers=_login(stack, "agent", surface="workbench"), timeout=10,
        trust_env=False,
    )
    assert r.status_code == 200, r.text
    runs = r.json()["runs"]
    assert runs and runs[-1].get("trace_id"), runs
    trace_id = runs[-1]["trace_id"]

    r = httpx.get(
        f"http://127.0.0.1:{stack['ports']['console']}/api/traces/{trace_id}",
        headers=_login(stack, "agent", surface="workbench"), timeout=10,
        trust_env=False,
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["trace_id"] == trace_id
    services = set(data["services"])
    assert {"console", "orchestrator", "customer"} <= services, data["services"]
    tree = data["tree"]
    assert tree["service"] == "console"  # 入口为根
    assert not tree.get("reparented")
    child_services = [c["service"] for c in tree["children"]]
    assert "orchestrator" in child_services, tree
    orch = next(c for c in tree["children"] if c["service"] == "orchestrator")
    assert any(c["service"] == "customer" for c in orch["children"]), tree
    assert any(c["name"] == "securityd POST /internal/audit/events"
               for c in tree["children"]), [c["name"] for c in tree["children"]]
