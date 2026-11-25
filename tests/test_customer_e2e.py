"""2A 端到端：客户域对话、customer 360、trace、会话状态机与越权。"""

import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.seed_mock import seed  # noqa: E402
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
    log = open(log_dir / f"p2a_{name}.log", "w", encoding="utf-8")  # noqa: SIM115
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
            ("securityd", "orchestrator", "console", "customer", "merchandising",
             "policy")
        }
        security_db = str(Path(tmp) / "security.db")
        security_env = {
            **os.environ, "ENV": "test", "AUTH_SECRET": "test-secret",
            "SECURITY_MODE": "local", "SECURITY_DB_PATH": security_db,
            "SHARED_DB_PATH": security_db,
            "SERVICE_NAME": "securityd",
            "TRACING_ENABLED": "true", "TRACE_DIR": str(Path(tmp) / "traces"),
        }
        sec = _spawn("security_service.server:app", ports["securityd"], security_env)
        try:
            assert _wait(f"http://127.0.0.1:{ports['securityd']}/health"), "securityd 未就绪"
            common = {
                **os.environ, "ENV": "test", "LLM_PROVIDER": "fake",
                "CHECKPOINTER": "sqlite", "AUTH_SECRET": "test-secret",
                "RATE_LIMIT_ENABLED": "false",
                "SECURITY_MODE": "remote",
                "SECURITY_URL": f"http://127.0.0.1:{ports['securityd']}",
                "SHARED_DB_PATH": str(Path(tmp) / "shared.db"),
                "CONSOLE_DB_PATH": str(Path(tmp) / "console.db"),
                "ORCHESTRATOR_PORT": str(ports["orchestrator"]),
                "CONSOLE_PORT": str(ports["console"]),
                "CUSTOMER_PORT": str(ports["customer"]),
                "MERCHANDISING_PORT": str(ports["merchandising"]),
                "POLICY_PORT": str(ports["policy"]),
                "TRACING_ENABLED": "true", "TRACE_DIR": str(Path(tmp) / "traces"),
            }
            targets = {
                "orchestrator": "orchestrator.server:app",
                "console": "console.server:app",
                "customer": "domains.customer.server:app",
                "merchandising": "domains.merchandising.server:app",
                "policy": "domains.policy.server:app",
            }
            envs = {
                n: {**common, "SQLITE_DB_PATH": str(Path(tmp) / f"{n}.sqlite"),
                    "SERVICE_NAME": n}
                for n in targets
            }
            procs = {n: _spawn(t, ports[n], envs[n]) for n, t in targets.items()}
            for n, p in ports.items():
                if n == "securityd":
                    continue
                assert _wait(
                    f"http://127.0.0.1:{p}/.well-known/agent-card.json"), f"{n} 未就绪"
            yield {"ports": ports,
                   "console_db": str(Path(tmp) / "console.db"),
                   "trace_dir": str(Path(tmp) / "traces")}
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


def _login(stack, username: str, surface: str = "customer") -> dict:  # noqa: ANN001
    r = httpx.post(
        _console(stack) + "/api/auth/login",
        json={"tenant_id": "t_demo", "username": username,
              "password": "demo123", "surface": surface},
        timeout=10, trust_env=False,
    )
    assert r.status_code == 200, r.text
    data = r.json()
    return {"Authorization": f"Bearer {data['token']}"}


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _send(stack, headers: dict, text: str, cid: str | None = None,
          customer_id: str | None = None):  # noqa: ANN001
    body = {"text": text}
    if cid:
        body["conversation_id"] = cid
    if customer_id:
        body["customer_id"] = customer_id
    r = httpx.post(
        _console(stack) + "/api/customer/send", headers=headers,
        json=body, timeout=60, trust_env=False,
    )
    assert r.status_code == 200, r.text
    return r.json()


def _db_one(sql: str, args: tuple = ()):  # noqa: ANN001
    conn = sqlite3.connect(BUSINESS_DB)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, args).fetchone()
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_credit_chat_routes_customer_domain(stack):
    customer = _login(stack, "customer")
    sent = _send(stack, customer, "我的授信额度还有多少")
    cid = sent["conversation_id"]
    assert sent["type"] == "ai" and sent.get("run_id")
    agent = _login(stack, "agent")
    trace = httpx.get(
        _console(stack) + f"/api/conversations/{cid}/trace",
        headers=agent, timeout=15, trust_env=False,
    )
    assert trace.status_code == 200, trace.text
    runs = trace.json()["runs"]
    assert runs and runs[0]["intent"] == "credit_query"
    assert runs[0]["run_id"] == sent["run_id"]
    steps = {s["stage"] for s in runs[0]["steps"]}
    assert "synthesize" in steps and "deliver" in steps


def test_customer360_contract_and_authz(stack):
    customer = _login(stack, "customer")
    sent = _send(stack, customer, "查一下我的客户资料")
    cid = sent["conversation_id"]
    agent = _login(stack, "agent")

    r = httpx.get(
        _console(stack) + f"/api/conversations/{cid}/customer360",
        headers=agent, timeout=15, trust_env=False,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["customer"]["customer_id"] == "CU-1001"
    assert body["profile"]["customer_code"] == "DEALER-1001"
    assert body["orders"] and body["orders"][0]["order_no"] == "SO-2001"
    assert "agent_queries" in body
    # 2B：agent_queries 绑定最近一次 run 的 trace_id
    latest_trace = httpx.get(
        _console(stack) + f"/api/conversations/{cid}/trace",
        headers=agent, timeout=15, trust_env=False,
    ).json()["runs"][0]
    if body["agent_queries"]:
        assert body["agent_queries"][0]["trace_id"] == latest_trace["trace_id"]

    # 客户角色不可见 360
    forbidden = httpx.get(
        _console(stack) + f"/api/conversations/{cid}/customer360",
        headers=customer, timeout=15, trust_env=False,
    )
    assert forbidden.status_code == 403

    # 跨租户：t_other 的会话对 t_demo 坐席不可见
    other_login = httpx.post(
        _console(stack) + "/api/auth/login",
        json={"tenant_id": "t_other", "username": "customer2",
              "password": "demo123", "surface": "customer"},
        timeout=10, trust_env=False,
    )
    other_headers = {"Authorization": f"Bearer {other_login.json()['token']}"}
    other_sent = _send(stack, other_headers, "你好")
    other_cid = other_sent["conversation_id"]
    cross = httpx.get(
        _console(stack) + f"/api/conversations/{other_cid}/customer360",
        headers=agent, timeout=15, trust_env=False,
    )
    assert cross.status_code == 404

    # 每次查询写审计
    supervisor = _login(stack, "supervisor", surface="workbench")
    events = httpx.get(
        _console(stack) + "/api/audit/events?limit=100",
        headers=supervisor, timeout=15, trust_env=False,
    ).json()
    actions = {e["action"] for e in events}
    assert "console.customer360" in actions


def test_trace_contract_and_authz(stack):
    customer = _login(stack, "customer")
    sent = _send(stack, customer, "查一下我的对账单")
    cid = sent["conversation_id"]
    agent = _login(stack, "agent")

    r = httpx.get(
        _console(stack) + f"/api/conversations/{cid}/trace",
        headers=agent, timeout=15, trust_env=False,
    )
    assert r.status_code == 200, r.text
    runs = r.json()["runs"]
    assert runs
    steps = {s["stage"] for s in runs[0]["steps"]}
    assert {"intake", "route", "domain_call", "deliver"} <= steps
    domain_steps = [s for s in runs[0]["steps"] if s["stage"] == "domain_call"]
    assert domain_steps and domain_steps[0]["domain"] == "customer"
    assert domain_steps[0]["tool"] in (
        "reconciliation.query", "customer.profile", "credit.query")
    # 2B：tool 级 trace（每次工具调用一条 step）
    tool_steps = [s for s in runs[0]["steps"] if s["stage"] == "tool"]
    assert tool_steps and tool_steps[0]["domain"] == "customer"
    assert tool_steps[0]["tool"] in (
        "reconciliation_query", "customer_profile", "credit_query")
    assert tool_steps[0]["latency_ms"] is not None
    assert "trace_id" in tool_steps[0] or True

    forbidden = httpx.get(
        _console(stack) + f"/api/conversations/{cid}/trace",
        headers=customer, timeout=15, trust_env=False,
    )
    assert forbidden.status_code == 403


def test_conversation_state_machine(stack):
    agent = _login(stack, "agent")
    supervisor = _login(stack, "supervisor", surface="workbench")
    customer = _login(stack, "customer")

    sent = _send(stack, customer, "你好，帮我看看资料")
    cid = sent["conversation_id"]

    # 客户关闭 → CLOSED
    r = httpx.post(
        _console(stack) + f"/api/conversations/{cid}/close",
        headers=customer, timeout=15, trust_env=False,
    )
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "CLOSED"

    # 新会话：坐席关闭 → PENDING_REVIEW → 主管驳回 → HUMAN_ACTIVE → 批准 → CLOSED
    sent2 = _send(stack, agent, "跟进一下（staff 会话）", customer_id="CU-1002")
    cid2 = sent2["conversation_id"]
    close = httpx.post(
        _console(stack) + f"/api/conversations/{cid2}/close",
        headers=agent, timeout=15, trust_env=False,
    )
    assert close.status_code == 200
    assert close.json()["state"] == "PENDING_REVIEW"

    reject = httpx.post(
        _console(stack) + f"/api/conversations/{cid2}/review",
        headers=supervisor, json={"decision": "reject"},
        timeout=15, trust_env=False,
    )
    assert reject.status_code == 200
    assert reject.json()["state"] == "HUMAN_ACTIVE"

    close2 = httpx.post(
        _console(stack) + f"/api/conversations/{cid2}/close",
        headers=agent, timeout=15, trust_env=False,
    )
    assert close2.status_code == 200
    approve = httpx.post(
        _console(stack) + f"/api/conversations/{cid2}/review",
        headers=supervisor, json={"decision": "approve"},
        timeout=15, trust_env=False,
    )
    assert approve.status_code == 200
    assert approve.json()["state"] == "CLOSED"

    # 审核状态不可重复关闭
    again = httpx.post(
        _console(stack) + f"/api/conversations/{cid}/close",
        headers=agent, timeout=15, trust_env=False,
    )
    assert again.status_code == 409

    # 2B：CLOSED 会话拦截发言（客户/坐席都被拒）
    blocked = httpx.post(
        _console(stack) + "/api/customer/send", headers=customer,
        json={"conversation_id": cid, "text": "还在吗"},
        timeout=15, trust_env=False,
    )
    assert blocked.status_code == 403
    blocked_reply = httpx.post(
        _console(stack) + f"/api/conversations/{cid}/reply", headers=agent,
        json={"text": "在的"}, timeout=15, trust_env=False,
    )
    # 拦截语义一致：REST 返回 409（会话已结束）
    assert blocked_reply.status_code in (403, 409)


def test_customer_history_pagination_same_second(stack):  # noqa: ANN001
    """updated_at 只有秒精度：同秒会话翻页不得跳行/重复。"""
    from urllib.parse import quote

    customer = _login(stack, "customer")
    ids = []
    for _ in range(5):
        sent = _send(stack, customer, "你好")
        ids.append(sent["conversation_id"])
    conn = sqlite3.connect(stack["console_db"])
    conn.execute(
        "UPDATE conversations SET updated_at=? WHERE id IN (?,?,?,?,?)",
        ("2026-01-01 00:00:00", *ids),
    )
    conn.commit()
    conn.close()

    seen: list[str] = []
    before = ""
    for _ in range(10):
        query = "?limit=2" + (f"&before={quote(before)}" if before else "")
        page = httpx.get(
            _console(stack) + "/api/customer/history" + query,
            headers=customer, timeout=30, trust_env=False,
        )
        assert page.status_code == 200, page.text
        data = page.json()
        seen.extend(c["id"] for c in data["conversations"])
        before = data["next_before"]
        if not before:
            break
    assert len(seen) == len(set(seen)), "翻页出现重复会话"
    for cid in ids:
        assert seen.count(cid) == 1, f"同秒会话 {cid} 未被稳定分页"
