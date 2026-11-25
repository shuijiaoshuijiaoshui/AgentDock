"""2B 端到端：附件 / 工单 / 知识库 / 统计 / 卡片 action + 越权。"""

import io
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.seed_mock import seed  # noqa: E402
from shared.auth import auth_config  # noqa: E402
from shared.config import get_settings  # noqa: E402

os.environ.setdefault("AUTH_SECRET", "test-secret")
BUSINESS_DB = ROOT / "infra" / "mockdata" / "mock.db"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _spawn(target: str, port: int, env: dict):  # noqa: ANN001, ANN201
    log_dir = ROOT / ".data" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    name = target.split(":")[0].replace(".", "_")
    log = open(log_dir / f"p2b_{name}.log", "w", encoding="utf-8")  # noqa: SIM115
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", target, "--port", str(port),
         "--log-level", "warning"],
        env=env, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
    )


def _wait(url: str, timeout: float = 40) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(url, timeout=2, trust_env=False).status_code == 200:
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.4)
    return False


@pytest.fixture(scope="module")
def stack():
    get_settings.cache_clear()
    seed()
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        ports = {
            n: _free_port() for n in
            ("securityd", "orchestrator", "console", "customer", "policy",
             "order", "fulfillment", "aftersale")
        }
        security_db = str(Path(tmp) / "security.db")
        sec = _spawn(
            "security_service.server:app", ports["securityd"],
            {**os.environ, "ENV": "test", "AUTH_SECRET": "test-secret",
             "SECURITY_MODE": "local", "SECURITY_DB_PATH": security_db,
             "SHARED_DB_PATH": security_db},
        )
        try:
            assert _wait(f"http://127.0.0.1:{ports['securityd']}/health")
            common = {
                **os.environ, "ENV": "test", "LLM_PROVIDER": "fake",
                "CHECKPOINTER": "sqlite", "AUTH_SECRET": "test-secret",
                "RATE_LIMIT_ENABLED": "false", "SECURITY_MODE": "remote",
                "SECURITY_URL": f"http://127.0.0.1:{ports['securityd']}",
                "SHARED_DB_PATH": str(Path(tmp) / "shared.db"),
                "CONSOLE_DB_PATH": str(Path(tmp) / "console.db"),
                "ORCHESTRATOR_PORT": str(ports["orchestrator"]),
                "CONSOLE_PORT": str(ports["console"]),
                "CUSTOMER_PORT": str(ports["customer"]),
                "POLICY_PORT": str(ports["policy"]),
                "ORDER_PORT": str(ports["order"]),
                "FULFILLMENT_PORT": str(ports["fulfillment"]),
                "AFTERSALE_PORT": str(ports["aftersale"]),
            }
            targets = {
                "orchestrator": "orchestrator.server:app",
                "console": "console.server:app",
                "customer": "domains.customer.server:app",
                "policy": "domains.policy.server:app",
                "order": "domains.order.server:app",
                "fulfillment": "domains.fulfillment.server:app",
                "aftersale": "domains.aftersale.server:app",
            }
            envs = {
                n: {**common, "SQLITE_DB_PATH": str(Path(tmp) / f"{n}.sqlite")}
                for n in targets
            }
            procs = {n: _spawn(t, ports[n], envs[n]) for n, t in targets.items()}
            for n, p in ports.items():
                if n == "securityd":
                    continue
                assert _wait(
                    f"http://127.0.0.1:{p}/.well-known/agent-card.json"), f"{n} 未就绪"
            yield {"ports": ports, "tmp": tmp}
            for proc in procs.values():
                proc.terminate()
            for proc in procs.values():
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
        finally:
            sec.terminate()
            try:
                sec.wait(timeout=5)
            except subprocess.TimeoutExpired:
                sec.kill()
            seed()


def _console(stack) -> str:  # noqa: ANN001
    return f"http://127.0.0.1:{stack['ports']['console']}"


def _login(stack, username: str, tenant: str = "t_demo") -> dict:  # noqa: ANN001
    r = httpx.post(
        _console(stack) + "/api/auth/login",
        json={"tenant_id": tenant, "username": username,
              "password": "demo123", "surface": "customer"},
        timeout=10, trust_env=False,
    )
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _png_bytes() -> bytes:
    return (b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)


def _upload(stack, headers: dict, content: bytes = b"", filename: str = "凭证.png",
            conversation_id: str | None = None):  # noqa: ANN001
    data = content or _png_bytes()
    form = {"file": ("凭证.png" if filename == "凭证.png" else filename,
                     io.BytesIO(data), "image/png")}
    if conversation_id:
        form["conversation_id"] = (None, conversation_id)
    return httpx.post(
        _console(stack) + "/api/attachments", headers=headers,
        files=form, timeout=30, trust_env=False,
    )


# ---------------------------------------------------------------------------
# 附件
# ---------------------------------------------------------------------------

def test_attachment_upload_download_and_authz(stack):
    customer = _login(stack, "customer")
    sent = _send(stack, customer, "凭证上传测试")
    cid = sent["conversation_id"]

    up = _upload(stack, customer, conversation_id=cid)
    assert up.status_code == 200, up.text
    body = up.json()
    assert body["mime"] == "image/png" and body["size"] == len(_png_bytes())
    assert len(body["sha256"]) == 64

    # 上传者下载：Content-Disposition 固定 attachment
    down = httpx.get(
        _console(stack) + f"/api/attachments/{body['attachment_id']}",
        headers=customer, timeout=15, trust_env=False,
    )
    assert down.status_code == 200
    assert down.headers["content-type"].startswith("image/png")
    assert down.headers["content-disposition"].startswith("attachment")
    assert down.content == _png_bytes()

    # 其他客户 → 403
    other = _login(stack, "customer3")
    forbidden = httpx.get(
        _console(stack) + f"/api/attachments/{body['attachment_id']}",
        headers=other, timeout=15, trust_env=False,
    )
    assert forbidden.status_code == 403

    # staff（同租户、会话可见）→ 200
    agent = _login(stack, "agent")
    staff_ok = httpx.get(
        _console(stack) + f"/api/attachments/{body['attachment_id']}",
        headers=agent, timeout=15, trust_env=False,
    )
    assert staff_ok.status_code == 200

    # 跨租户上传者下载自己的 → 404（tenant 隔离）
    other_tenant = _login(stack, "customer2", tenant="t_other")
    cross = _upload(stack, other_tenant)
    cross_id = cross.json()["attachment_id"]
    stolen = httpx.get(
        _console(stack) + f"/api/attachments/{cross_id}",
        headers=customer, timeout=15, trust_env=False,
    )
    assert stolen.status_code == 404

    # 路径穿越 → 客户端规范化后 404 / 服务端 400
    traversal = httpx.get(
        _console(stack) + "/api/attachments/..%2F..%2Fsecret",
        headers=customer, timeout=15, trust_env=False,
    )
    assert traversal.status_code in (400, 404)


def test_attachment_rejects_bad_content_and_size(stack):
    customer = _login(stack, "customer")
    fake_png = _upload(stack, customer, content=b"just plain text",
                       filename="fake.png")
    assert fake_png.status_code == 400
    assert fake_png.json()["error"] == "unsupported_type"

    big = _upload(stack, customer, content=_png_bytes() + b"\x00" * (10 * 1024 * 1024))
    assert big.status_code == 400
    assert big.json()["error"] == "file_too_large"

    # 读前限量：Content-Length 超限直接 400（不进内存）
    oversized = _png_bytes() + b"\x00" * (11 * 1024 * 1024)
    pre = httpx.post(
        _console(stack) + "/api/attachments", headers=customer,
        files={"file": ("big.png", io.BytesIO(oversized), "image/png")},
        timeout=30, trust_env=False,
    )
    assert pre.status_code == 400
    assert pre.json()["error"] == "file_too_large"


def test_attachment_message_binding(stack):
    customer = _login(stack, "customer")
    up = _upload(stack, customer).json()
    sent = _send(stack, customer, "查询订单 SO-2001 的状态",
                 attachment_ids=[up["attachment_id"]])
    assert sent["type"] == "ai"
    agent = _login(stack, "agent")
    msgs = httpx.get(
        _console(stack) + f"/api/conversations/{sent['conversation_id']}/messages",
        headers=agent, timeout=15, trust_env=False,
    ).json()
    mine = next(m for m in msgs if m["role"] == "customer")
    assert mine["meta"]["attachments"][0]["attachment_id"] == up["attachment_id"]
    # 未授权附件引用 → 403
    other = _login(stack, "customer3")
    other_up = _upload(stack, other).json()
    stolen = httpx.post(
        _console(stack) + "/api/customer/send", headers=customer,
        json={"text": "用别人的附件",
              "attachment_ids": [other_up["attachment_id"]]},
        timeout=30, trust_env=False,
    )
    assert stolen.status_code == 403


def _send(stack, headers: dict, text: str, cid: str | None = None,
          customer_id: str | None = None, attachment_ids: list | None = None):
    body = {"text": text}
    if cid:
        body["conversation_id"] = cid
    if customer_id:
        body["customer_id"] = customer_id
    if attachment_ids:
        body["attachment_ids"] = attachment_ids
    r = httpx.post(
        _console(stack) + "/api/customer/send", headers=headers,
        json=body, timeout=60, trust_env=False,
    )
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# 工单
# ---------------------------------------------------------------------------

def test_ticket_crud_assign_and_authz(stack):
    agent = _login(stack, "agent")
    customer = _login(stack, "customer")

    created = httpx.post(
        _console(stack) + "/api/tickets", headers=agent,
        json={"customer_id": "CU-1001", "sku": "SKU-A001",
              "fault_type": "按键失灵", "priority": "high",
              "department": "warehouse", "order_no": "SO-2001",
              "description": "2B 工单合同测试"},
        timeout=15, trust_env=False,
    )
    assert created.status_code == 201, created.text
    ticket_no = created.json()["ticket_no"]

    # 客户角色 403
    forbidden = httpx.post(
        _console(stack) + "/api/tickets", headers=customer,
        json={"customer_id": "CU-1001", "sku": "SKU-A001"},
        timeout=15, trust_env=False,
    )
    assert forbidden.status_code == 403

    # 未知名客户 / 跨客户订单 → 404
    bad_customer = httpx.post(
        _console(stack) + "/api/tickets", headers=agent,
        json={"customer_id": "CU-9999", "sku": "SKU-A001"},
        timeout=15, trust_env=False,
    )
    assert bad_customer.status_code == 404
    cross_order = httpx.post(
        _console(stack) + "/api/tickets", headers=agent,
        json={"customer_id": "CU-1001", "sku": "SKU-A001",
              "order_no": "SO-2002"},
        timeout=15, trust_env=False,
    )
    assert cross_order.status_code == 404

    # 分派 + SLA + 状态流转
    assigned = httpx.post(
        _console(stack) + f"/api/tickets/{ticket_no}/assign", headers=agent,
        json={"department": "finance", "priority": "high",
              "sla_due": "2026-11-01 18:00:00"},
        timeout=15, trust_env=False,
    )
    assert assigned.status_code == 200, assigned.text
    assert assigned.json()["department"] == "finance"
    assert assigned.json()["status"] == "received"

    patched = httpx.patch(
        _console(stack) + f"/api/tickets/{ticket_no}", headers=agent,
        json={"status": "resolved"}, timeout=15, trust_env=False,
    )
    assert patched.status_code == 200
    assert patched.json()["status"] == "resolved"

    # 坏状态 / 坏部门 → 400
    bad_status = httpx.patch(
        _console(stack) + f"/api/tickets/{ticket_no}", headers=agent,
        json={"status": "finished"}, timeout=15, trust_env=False,
    )
    assert bad_status.status_code == 400
    bad_dept = httpx.post(
        _console(stack) + f"/api/tickets/{ticket_no}/assign", headers=agent,
        json={"department": "sales"}, timeout=15, trust_env=False,
    )
    assert bad_dept.status_code == 400

    # 跨租户 → 404
    other_agent = _login(stack, "agent2", tenant="t_other")
    cross_tenant = httpx.patch(
        _console(stack) + f"/api/tickets/{ticket_no}", headers=other_agent,
        json={"status": "closed"}, timeout=15, trust_env=False,
    )
    assert cross_tenant.status_code == 404

    # 列表 + 审计
    listed = httpx.get(
        _console(stack) + "/api/tickets?customer_id=CU-1001", headers=agent,
        timeout=15, trust_env=False,
    ).json()["tickets"]
    assert any(t["ticket_no"] == ticket_no for t in listed)
    supervisor = _login(stack, "supervisor")
    events = httpx.get(
        _console(stack) + "/api/audit/events?limit=200", headers=supervisor,
        timeout=15, trust_env=False,
    ).json()
    actions = {e["action"] for e in events}
    assert {"console.ticket_create", "console.ticket_assign",
            "console.ticket_update"} <= actions


# ---------------------------------------------------------------------------
# 知识库
# ---------------------------------------------------------------------------

def test_knowledge_upsert_versions_and_authz(stack):
    # Phase 4：发布/归档仅 supervisor/admin；agent 只能 draft/submitted
    agent = _login(stack, "agent")
    denied = httpx.post(
        _console(stack) + "/api/knowledge", headers=agent,
        json={"title": "2B 测试条目", "content": "测试内容",
              "scope": "general", "status": "published"},
        timeout=15, trust_env=False,
    )
    assert denied.status_code == 403
    supervisor = _login(stack, "supervisor")
    created = httpx.post(
        _console(stack) + "/api/knowledge", headers=supervisor,
        json={"title": "2B 测试条目", "content": "测试内容",
              "scope": "general", "status": "published", "keywords": "测试"},
        timeout=15, trust_env=False,
    )
    assert created.status_code == 201, created.text
    entry_id = created.json()["entry_id"]

    updated = httpx.post(
        _console(stack) + "/api/knowledge", headers=supervisor,
        json={"entry_id": entry_id, "title": "2B 测试条目",
              "content": "修订内容", "status": "published"},
        timeout=15, trust_env=False,
    )
    assert updated.status_code == 201
    assert updated.json()["version"] == "v2"

    versions = httpx.get(
        _console(stack) + f"/api/knowledge/{entry_id}/versions", headers=supervisor,
        timeout=15, trust_env=False,
    ).json()["versions"]
    assert {v["version"] for v in versions} >= {"v1", "v2"}

    customer = _login(stack, "customer")
    forbidden = httpx.post(
        _console(stack) + "/api/knowledge", headers=customer,
        json={"title": "x", "content": "y"}, timeout=15, trust_env=False,
    )
    assert forbidden.status_code == 403


def test_policy_override_on_conflict(stack):
    agent = _login(stack, "agent")
    # 退款到账 → 权威条款 CLS-005 + 冲突知识 KB-1002 → policy_override=true
    sent = _send(stack, agent, "到账工作日政策是什么", customer_id="CU-1001")
    assert sent["type"] == "ai"
    supervisor = _login(stack, "supervisor")
    events = httpx.get(
        _console(stack) + "/api/audit/events?limit=300", headers=supervisor,
        timeout=15, trust_env=False,
    ).json()
    assert any(e["action"] == "knowledge.override" for e in events)


# ---------------------------------------------------------------------------
# 统计
# ---------------------------------------------------------------------------

def test_stats_summary_real_aggregation(stack):
    supervisor = _login(stack, "supervisor")
    r = httpx.get(
        _console(stack) + "/api/stats/summary", headers=supervisor,
        timeout=30, trust_env=False,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total_conversations"] > 0
    assert isinstance(body["intent_distribution"], dict)
    assert body["intent_distribution"]
    assert 0.0 <= body["handoff_rate"] <= 1.0
    assert 0.0 <= body["ai_handled_rate"] <= 1.0
    customer = _login(stack, "customer")
    forbidden = httpx.get(
        _console(stack) + "/api/stats/summary", headers=customer,
        timeout=15, trust_env=False,
    )
    assert forbidden.status_code == 403


# ---------------------------------------------------------------------------
# 卡片 + action registry
# ---------------------------------------------------------------------------

def test_order_card_and_action_registry(stack):
    customer = _login(stack, "customer")
    sent = _send(stack, customer, "查询订单 SO-2001 的状态")
    cid = sent["conversation_id"]
    agent = _login(stack, "agent")
    msgs = httpx.get(
        _console(stack) + f"/api/conversations/{cid}/messages",
        headers=agent, timeout=15, trust_env=False,
    ).json()
    ai_msg = next(m for m in msgs if m["role"] == "ai")
    # 客户可见回复用域摘要，不回显内部 JSON 字段
    assert "订单 SO-2001" in ai_msg["content"]
    for internal in ("tenant_id", "customer_id", "tool_events", "snapshot_id"):
        assert internal not in ai_msg["content"]
    cards = (ai_msg.get("meta") or {}).get("cards") or []
    order_cards = [c for c in cards if c["type"] == "order"]
    assert order_cards and order_cards[0]["title"] == "订单 SO-2001"
    action_ids = {a["action_id"] for a in order_cards[0]["actions"]}
    assert {"order.track", "aftersale.eligibility"} <= action_ids

    # 卡片按钮：白名单 action_id + 签名 action token
    acted = httpx.post(
        _console(stack) + f"/api/conversations/{cid}/card-action",
        headers=customer,
        json={"action_id": "order.track", "params": {"order_no": "SO-2001"}},
        timeout=60, trust_env=False,
    )
    assert acted.status_code == 200, acted.text
    reply = acted.json()["reply"]
    # 履约摘要应说清承运/运单/节点，而不是回显内部 JSON
    assert any(word in reply for word in ("物流", "承运", "运单", "fulfill"))
    assert "tenant_id" not in reply

    # 未注册 action_id → 400
    unknown = httpx.post(
        _console(stack) + f"/api/conversations/{cid}/card-action",
        headers=customer, json={"action_id": "system.shell",
                                "params": {"cmd": "rm -rf /"}},
        timeout=15, trust_env=False,
    )
    assert unknown.status_code == 400
    assert unknown.json()["error"] == "unknown_action"

    # 缺必填参数 → 400
    missing = httpx.post(
        _console(stack) + f"/api/conversations/{cid}/card-action",
        headers=customer, json={"action_id": "order.track", "params": {}},
        timeout=15, trust_env=False,
    )
    assert missing.status_code == 400

    # params 非 dict → 400
    bad_params = httpx.post(
        _console(stack) + f"/api/conversations/{cid}/card-action",
        headers=customer,
        json={"action_id": "order.track", "params": ["order_no", "SO-2001"]},
        timeout=15, trust_env=False,
    )
    assert bad_params.status_code == 400
    assert bad_params.json()["error"] == "bad_params"


# ---------------------------------------------------------------------------
# 单元：policy_override 检索语义
# ---------------------------------------------------------------------------

def test_policy_override_unit():
    from domains.policy.tools import answer_policy

    seed()
    cfg = auth_config("policy.read", role="customer", actor_id="CU-1001")
    out = json.loads(answer_policy.invoke({"query": "退款到账"}, config=cfg))
    assert out["policy_override"] is True
    assert any(c["clause_id"] == "CLS-005" for c in out["clauses"])
    assert any(k["entry_id"] == "KB-1002" for k in out["knowledge"])
