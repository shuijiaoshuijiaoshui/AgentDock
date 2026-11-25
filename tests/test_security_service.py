"""securityd：单写者并发、服务间认证、审批/确认单生命周期。"""

import concurrent.futures
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

from shared import approvals, audit, confirmations  # noqa: E402
from shared.auth import create_token  # noqa: E402
from shared.config import get_settings  # noqa: E402

os.environ.setdefault("AUTH_SECRET", "test-secret")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="module")
def securityd():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        port = _free_port()
        db = str(Path(tmp) / "security.db")
        env = {
            **os.environ,
            "ENV": "test",
            "SECURITY_MODE": "local",
            "SECURITY_DB_PATH": db,
            "SHARED_DB_PATH": db,
            "AUTH_SECRET": "test-secret",
        }
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "security_service.server:app",
             "--port", str(port), "--log-level", "warning"],
            env=env, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        base = f"http://127.0.0.1:{port}"
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                if httpx.get(base + "/health", timeout=1, trust_env=False).status_code == 200:
                    break
            except Exception:  # noqa: BLE001
                time.sleep(0.3)
        else:
            proc.terminate()
            pytest.fail("securityd 未就绪")
        yield {"base": base, "db": db, "port": port}
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture()
def remote_security(securityd, monkeypatch):
    monkeypatch.setenv("SECURITY_MODE", "remote")
    monkeypatch.setenv("SECURITY_URL", securityd["base"])
    get_settings.cache_clear()
    yield securityd
    get_settings.cache_clear()


def _token(scope: str, *, aud: str = "securityd", role: str = "service") -> str:
    return create_token(
        tenant_id="security", actor_id="tester", role=role,
        scopes=[scope], audience=aud, token_type="service",
    )


def _headers(scope: str = "security.write") -> dict:
    return {"Authorization": f"Bearer {_token(scope)}"}


def test_securityd_rejects_missing_user_and_wrong_audience(securityd):
    base = securityd["base"]
    assert httpx.post(base + "/internal/audit/events", json={},
                      trust_env=False).status_code == 401
    user = create_token(
        tenant_id="t_demo", actor_id="CU-1001", role="customer",
        scopes=["order.read"],
    )
    assert httpx.post(
        base + "/internal/audit/events", json={},
        headers={"Authorization": f"Bearer {user}"}, trust_env=False,
    ).status_code == 401
    no_aud = create_token(
        tenant_id="security", actor_id="tester", role="service",
        scopes=["security.write"], token_type="service",
    )
    assert httpx.post(
        base + "/internal/audit/events", json={},
        headers={"Authorization": f"Bearer {no_aud}"}, trust_env=False,
    ).status_code == 401
    read_only = _token("security.read")
    assert httpx.post(
        base + "/internal/audit/events", json={},
        headers={"Authorization": f"Bearer {read_only}"}, trust_env=False,
    ).status_code == 403


def _audit_payload(i: int, prefix: str, tenant: str = "t_demo") -> dict:
    return {
        "event_id": f"{prefix}-{i}",
        "tenant_id": tenant,
        "actor_id": "CU-1001",
        "actor_role": "customer",
        "action": "tool.call",
        "target": "order.query",
        "params": {"i": i},
        "result_summary": "ok",
    }


def _audit_worker(args):  # noqa: ANN001
    base, prefix, n = args
    token = create_token(
        tenant_id="security", actor_id="worker", role="service",
        scopes=["security.write"], audience="securityd", token_type="service",
    )
    headers = {"Authorization": f"Bearer {token}"}
    with httpx.Client(timeout=20, trust_env=False) as client:
        for i in range(n):
            r = client.post(base + "/internal/audit/events", headers=headers,
                            json=_audit_payload(i, prefix))
            r.raise_for_status()
    return n


def test_audit_concurrency_8_processes_x_50(securityd):
    base = securityd["base"]
    workers = 8
    per_worker = 50
    with concurrent.futures.ProcessPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(_audit_worker, (base, f"w{w}", per_worker))
            for w in range(workers)
        ]
        assert sum(f.result() for f in futures) == workers * per_worker

    headers = _headers("security.read")
    events = httpx.get(
        base + "/internal/audit/events", headers=headers,
        params={"tenant_id": "t_demo", "limit": 500}, trust_env=False,
        timeout=20,
    ).json()["events"]
    assert len(events) == workers * per_worker
    ids = {e["event_id"] for e in events}
    assert len(ids) == workers * per_worker
    verify = httpx.get(
        base + "/internal/audit/verify", headers=headers,
        params={"tenant_id": "t_demo"}, trust_env=False, timeout=20,
    ).json()
    assert verify["ok"] is True and verify["total"] == workers * per_worker


def test_audit_verify_detects_tamper_and_tail_delete(securityd):
    base = securityd["base"]
    headers = _headers("security.read")
    db = securityd["db"]
    # 追加一条并确认链绿
    r = httpx.post(
        base + "/internal/audit/events", headers=_headers("security.write"),
        json=_audit_payload(999, "tamper", tenant="t_tamper"), trust_env=False,
    )
    assert r.status_code == 200
    assert httpx.get(
        base + "/internal/audit/verify", headers=headers,
        params={"tenant_id": "t_tamper"}, trust_env=False,
    ).json()["ok"] is True

    conn = sqlite3.connect(db, timeout=10)
    try:
        with conn:
            conn.execute(
                "UPDATE audit_events SET result_summary='tampered' "
                "WHERE event_id='tamper-999'"
            )
        got = httpx.get(
            base + "/internal/audit/verify", headers=headers,
            params={"tenant_id": "t_tamper"}, trust_env=False,
        ).json()
        assert got["ok"] is False
        with conn:
            conn.execute("DELETE FROM audit_events WHERE event_id='tamper-999'")
        got = httpx.get(
            base + "/internal/audit/verify", headers=headers,
            params={"tenant_id": "t_tamper"}, trust_env=False,
        ).json()
        # 链尾删除：head 的 count/last_hash 与现存事件不一致
        assert got["ok"] is False
    finally:
        conn.close()


def test_approval_lifecycle_expiry_and_binding(remote_security):
    created = approvals.create_approval(
        tenant_id="t_demo", action="refund", target="SO-2001", amount=6000.0,
        requested_by="CU-1001", requested_role="customer",
        required_approvals=1, approver_roles=("finance",), dual_control=False,
        reason="test",
    )
    aid = created["id"]
    assert str(aid).startswith("APR-")

    assert approvals.approve(
        "t_demo", aid, actor_id="CU-1001", role="customer",
    )["error"] == "self_approval_forbidden"
    approved = approvals.approve(
        "t_demo", aid, actor_id="FIN-001", role="finance",
    )
    assert approved["status"] == "approved"

    wrong_amount = approvals.consume(
        "t_demo", aid, action="refund", target="SO-2001", amount=1.0,
        actor_id="CU-1001", role="customer",
    )
    assert wrong_amount["error"] == "approval_amount_mismatch"
    wrong_actor = approvals.consume(
        "t_demo", aid, action="refund", target="SO-2001", amount=6000.0,
        actor_id="AG-001", role="agent",
    )
    assert wrong_actor["error"] == "actor_not_allowed"
    first = approvals.consume(
        "t_demo", aid, action="refund", target="SO-2001", amount=6000.0,
        actor_id="CU-1001", role="customer",
    )
    assert first["ok"] is True
    second = approvals.consume(
        "t_demo", aid, action="refund", target="SO-2001", amount=6000.0,
        actor_id="CU-1001", role="customer",
    )
    assert second["ok"] is False and second["error"] == "status_executed"

    expired = approvals.create_approval(
        tenant_id="t_demo", action="refund", target="SO-2", amount=6000.0,
        requested_by="CU-1001", requested_role="customer",
        required_approvals=1, approver_roles=("finance",), dual_control=False,
        ttl_minutes=-1,
    )
    assert approvals.approve(
        "t_demo", expired["id"], actor_id="FIN-001", role="finance",
    )["error"] == "expired"
    assert approvals.consume(
        "t_demo", expired["id"], action="refund", target="SO-2", amount=6000.0,
        actor_id="CU-1001", role="customer",
    )["ok"] is False


def test_confirmation_lifecycle(remote_security):
    created = confirmations.create_confirmation(
        tenant_id="t_demo", actor_id="CU-1001", customer_id="CU-1001",
        action="ticket.create", target="SKU-A001:按键失灵",
        payload={"sku": "SKU-A001"}, ttl_seconds=600,
    )
    token = created["confirm_token"]
    assert confirmations.consume_confirmation(
        token, tenant_id="t_demo", actor_id="CU-1001", customer_id="CU-1001",
        action="ticket.create", target="SKU-A001:按键失灵",
        payload={"sku": "SKU-A001"},
    )["ok"] is True
    replay = confirmations.consume_confirmation(
        token, tenant_id="t_demo", actor_id="CU-1001", customer_id="CU-1001",
        action="ticket.create", target="SKU-A001:按键失灵",
        payload={"sku": "SKU-A001"},
    )
    assert replay["ok"] is False and replay["error"] == "status_consumed"

    other = confirmations.create_confirmation(
        tenant_id="t_demo", actor_id="CU-1001", customer_id="CU-1001",
        action="ticket.create", target="SKU-A001", payload={"sku": "SKU-A001"},
    )
    bad = confirmations.consume_confirmation(
        other["confirm_token"], tenant_id="t_demo", actor_id="CU-1002",
        customer_id="CU-1002", action="ticket.create", target="SKU-A001",
        payload={"sku": "SKU-A001"},
    )
    assert bad["ok"] is False and bad["error"] == "confirmation_scope_mismatch"


def test_remote_facade_uses_securityd(remote_security):
    eid = audit.audit_event(
        tenant_id="t_demo", actor_id="CU-1001", actor_role="customer",
        action="tool.call", target="order.query", params={"x": 1},
        event_id=f"facade-{uuid.uuid4().hex}",
    )
    assert isinstance(eid, int)
    events = audit.list_events("t_demo", limit=10)
    assert any(e["action"] == "tool.call" for e in events)
    assert audit.verify_chain("t_demo")["ok"] is True

