"""1B 端到端：改地址/建工单确认落库 + 高价值退款审批回到对话执行。"""

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
    log = open(log_dir / f"confirm_{name}.log", "w", encoding="utf-8")  # noqa: SIM115
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
        time.sleep(0.5)
    return False


@pytest.fixture(scope="module")
def stack():
    get_settings.cache_clear()
    seed()
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        ports = {
            n: _free_port() for n in
            ("securityd", "orchestrator", "console", "order", "fulfillment",
             "aftersale", "finance")
        }
        security_db = str(Path(tmp) / "security.db")
        security_env = {
            **os.environ, "ENV": "test", "AUTH_SECRET": "test-secret",
            "SECURITY_MODE": "local", "SECURITY_DB_PATH": security_db,
            "SHARED_DB_PATH": security_db,
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
                "ORDER_PORT": str(ports["order"]),
                "FULFILLMENT_PORT": str(ports["fulfillment"]),
                "AFTERSALE_PORT": str(ports["aftersale"]),
                "FINANCE_PORT": str(ports["finance"]),
            }
            targets = {
                "orchestrator": "orchestrator.server:app",
                "console": "console.server:app",
                "order": "domains.order.server:app",
                "fulfillment": "domains.fulfillment.server:app",
                "aftersale": "domains.aftersale.server:app",
                "finance": "domains.finance.server:app",
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
            yield {"ports": ports}
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


def _login(stack, username: str, surface: str = "customer") -> str:  # noqa: ANN001
    r = httpx.post(
        _console(stack) + "/api/auth/login",
        json={"tenant_id": "t_demo", "username": username, "password": "demo123",
              "surface": surface},
        timeout=10, trust_env=False,
    )
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _send(stack, token: str, text: str, cid: str | None = None):  # noqa: ANN001
    r = httpx.post(
        _console(stack) + "/api/customer/send", headers=_h(token),
        json={"text": text, **({"conversation_id": cid} if cid else {})},
        timeout=60, trust_env=False,
    )
    assert r.status_code == 200, r.text
    return r.json()


def _resume(stack, token: str, cid: str, action_id: str):  # noqa: ANN001
    r = httpx.post(
        _console(stack) + f"/api/conversations/{cid}/actions", headers=_h(token),
        json={"action_id": action_id}, timeout=60, trust_env=False,
    )
    return r


def _db_one(sql: str, args: tuple = ()):  # noqa: ANN001
    conn = sqlite3.connect(BUSINESS_DB)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, args).fetchone()
    finally:
        conn.close()


def test_address_confirm_round_trip(stack):
    token = _login(stack, "customer4")
    sent = _send(
        stack, token,
        "帮我把 SO-2004 改地址，改到 深圳市南山区新地址 1 号",
    )
    cid = sent["conversation_id"]
    pending = sent.get("pending_actions") or []
    assert pending and pending[0]["type"] == "confirm", sent
    resumed = _resume(stack, token, cid, pending[0]["action_id"])
    assert resumed.status_code == 200, resumed.text
    row = _db_one(
        "SELECT address FROM orders WHERE tenant_id='t_demo' AND order_no='SO-2004'"
    )
    assert "深圳市南山区新地址" in row["address"]


def test_ticket_confirm_round_trip(stack):
    token = _login(stack, "customer")
    sent = _send(
        stack, token, "为 SKU-A001 创建维修工单，故障：按键失灵",
    )
    cid = sent["conversation_id"]
    pending = sent.get("pending_actions") or []
    assert pending and pending[0]["type"] == "confirm"
    resumed = _resume(stack, token, cid, pending[0]["action_id"])
    assert resumed.status_code == 200, resumed.text
    row = _db_one(
        "SELECT ticket_no FROM tickets WHERE tenant_id='t_demo' "
        "AND customer_id='CU-1001' AND sku='SKU-A001' AND status='created' "
        "ORDER BY created_at DESC LIMIT 1"
    )
    assert row is not None and row["ticket_no"].startswith("TK-")


def test_low_value_refund_also_requires_approval(stack):
    """退款资金高风险：199 元（低于旧阈值 500）也必须人工审批后才落退款单。"""
    customer = _login(stack, "customer")
    sent = _send(stack, customer, "为订单 SO-2001 执行退款")
    cid = sent["conversation_id"]
    pending = sent.get("pending_actions") or []
    assert pending and pending[0]["type"] == "approval"
    # 未审批前不得有退款单
    assert _db_one(
        "SELECT 1 FROM refunds WHERE tenant_id='t_demo' AND order_no='SO-2001'"
    ) is None
    finance = _login(stack, "finance")
    rows = httpx.get(
        _console(stack) + "/api/approvals?status=pending",
        headers=_h(finance), timeout=10, trust_env=False,
    ).json()
    approval_id = next(
        (row["id"] for row in rows if row.get("target") == "SO-2001"), None)
    assert approval_id is not None
    approved = httpx.post(
        _console(stack) + f"/api/approvals/{approval_id}/approve",
        headers=_h(finance), timeout=10, trust_env=False,
    )
    assert approved.status_code == 200, approved.text
    executed = httpx.post(
        _console(stack) + f"/api/approvals/{approval_id}/execute",
        headers=_h(finance), timeout=60, trust_env=False,
    )
    assert executed.status_code == 200, executed.text
    refund = _db_one(
        "SELECT amount, status FROM refunds WHERE tenant_id='t_demo' "
        "AND order_no='SO-2001'"
    )
    assert refund is not None
    assert float(refund["amount"]) == 199.0
    assert refund["status"] == "settled"
    del cid


def test_high_value_refund_approval_round_trip(stack):
    customer = _login(stack, "customer3")
    sent = _send(stack, customer, "为订单 SO-2006 执行退款")
    cid = sent["conversation_id"]
    pending = sent.get("pending_actions") or []
    assert pending and pending[0]["type"] == "approval"
    approval_id = None
    rows = httpx.get(
        _console(stack) + "/api/approvals?status=pending",
        headers=_h(_login(stack, "finance")), timeout=10, trust_env=False,
    ).json()
    for row in rows:
        if row.get("target") == "SO-2006":
            approval_id = row["id"]
    assert approval_id is not None
    finance = _login(stack, "finance")
    approved = httpx.post(
        _console(stack) + f"/api/approvals/{approval_id}/approve",
        headers=_h(finance), timeout=10, trust_env=False,
    )
    assert approved.status_code == 200, approved.text
    executed = httpx.post(
        _console(stack) + f"/api/approvals/{approval_id}/execute",
        headers=_h(finance), timeout=60, trust_env=False,
    )
    assert executed.status_code == 200, executed.text
    refund = _db_one(
        "SELECT amount, status FROM refunds WHERE tenant_id='t_demo' "
        "AND order_no='SO-2006'"
    )
    assert refund is not None
    assert float(refund["amount"]) == 1200.0
    assert refund["status"] == "settled"
    # 2B：资金写链完结自动 PENDING_REVIEW（复核策略口径）
    supervisor = _h(_login(stack, "supervisor", surface="workbench"))
    conv = httpx.get(
        _console(stack) + "/api/conversations", headers=supervisor,
        timeout=15, trust_env=False,
    ).json()
    mine = next(c for c in conv if c["id"] == cid)
    assert mine["state"] == "PENDING_REVIEW"
