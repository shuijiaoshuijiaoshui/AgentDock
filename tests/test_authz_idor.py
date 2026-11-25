"""1A 对象级越权：客户 A 不能读写客户 B 的订单/工单/退款/发票/账单/物流。

覆盖两条路径：
1) 工具单测（直接 invoke）；
2) 真服务进程：直连域 A2A + 经编排 A2A。
"""

import json
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
os.environ.setdefault("AUTH_SECRET", "test-secret")

from a2a.types import TaskState  # noqa: E402

from domains.aftersale.tools import judge_eligibility, query_ticket  # noqa: E402
from domains.finance.tools import (  # noqa: E402
    billing_statement,
    invoice_issue,
    refund_settle,
    refund_status_query,
)
from domains.fulfillment.tools import fulfill_track  # noqa: E402
from domains.order.tools import cancel_order, order_amend, order_query  # noqa: E402
from scripts.seed_mock import seed  # noqa: E402
from shared.auth import (  # noqa: E402
    ALL_SCOPES,
    ROLE_SCOPES,
    auth_config,
    create_token,
    decode_token,
)
from shared.config import get_settings  # noqa: E402

CUSTOMER_A = auth_config(
    *ROLE_SCOPES["customer"], role="customer", actor_id="CU-1001",
)
CUSTOMER_1004 = auth_config(
    *ROLE_SCOPES["customer"], role="customer", actor_id="CU-1004",
)
STAFF = auth_config(*ROLE_SCOPES["agent"], role="agent", actor_id="AG-001")
STAFF_OWNER = auth_config(*ROLE_SCOPES["agent"], role="agent", actor_id="AG-001")
STAFF_OWNER["metadata"]["customer_id"] = "CU-1001"


@pytest.fixture(autouse=True)
def _seed():
    seed()
    yield
    seed()


def _json(tool, args, cfg):  # noqa: ANN001
    return json.loads(tool.invoke(args, config=cfg))


# ---------------------------------------------------------------------------
# 工具单测：跨客户对象全部 not_found
# ---------------------------------------------------------------------------

def test_customer_cannot_query_other_order():
    out = _json(order_query, {"order_no": "SO-2002"}, CUSTOMER_A)
    assert out["error"] == "not_found"


def test_customer_cannot_amend_other_order():
    out = _json(order_amend, {"order_no": "SO-2002", "address": "x"}, CUSTOMER_A)
    assert out["error"] == "not_found"


def test_customer_cannot_cancel_other_order():
    out = _json(cancel_order, {"order_no": "SO-2002", "reason": "test"}, CUSTOMER_A)
    assert out["error"] == "not_found"


def test_customer_cannot_read_other_billing():
    out = _json(billing_statement, {"order_no": "SO-2002"}, CUSTOMER_A)
    assert out["error"] == "not_found"


def test_customer_cannot_read_other_refund():
    out = _json(refund_status_query, {"order_no": "SO-2002"}, CUSTOMER_A)
    assert out["error"] == "not_found"


def test_customer_cannot_settle_other_refund():
    out = _json(refund_settle, {"order_no": "SO-2002"}, CUSTOMER_A)
    assert out["error"] == "not_found"


def test_customer_cannot_issue_invoice_for_other_order():
    out = _json(invoice_issue, {"order_no": "SO-2002", "title": "x"}, CUSTOMER_A)
    assert out["error"] == "not_found"


def test_customer_cannot_query_other_ticket():
    out = _json(query_ticket, {"ticket_no": "TK-3002"}, CUSTOMER_A)
    assert out["error"] == "not_found"


def test_customer_cannot_track_other_order():
    out = _json(fulfill_track, {"order_no": "SO-2002"}, CUSTOMER_A)
    assert out["error"] == "not_found"


def test_customer_cannot_judge_other_purchase():
    out = _json(
        judge_eligibility, {"sku": "SKU-A001", "order_no": "SO-2002"}, CUSTOMER_A
    )
    assert out["error"] == "not_found"


def test_customer_cannot_forge_customer_id():
    out = _json(
        order_query, {"order_no": "SO-2001", "customer_id": "CU-1002"}, CUSTOMER_A
    )
    assert out["error"] == "not_found"


def test_staff_without_customer_id_is_rejected():
    out = _json(order_query, {"order_no": "SO-2002"}, STAFF)
    assert out["error"] == "customer_id_required"


def test_staff_with_wrong_customer_is_rejected():
    out = _json(order_query, {"order_no": "SO-2002"}, STAFF_OWNER)
    assert out["error"] == "not_found"


def test_customer_can_query_own_order():
    out = _json(order_query, {"order_no": "SO-2001"}, CUSTOMER_A)
    assert out["order_no"] == "SO-2001"


def test_negative_price_is_rejected():
    out = _json(
        order_amend, {"order_no": "SO-2004", "address": "", "new_price": -1},
        CUSTOMER_1004,
    )
    assert out["error"] == "invalid_price"


def test_service_token_on_behalf_of_is_bound():
    token = create_token(
        tenant_id="t_demo", actor_id="svc-bound", role="service",
        scopes=ALL_SCOPES, on_behalf_of="CU-1001", token_type="service",
    )
    # 绑定 CU-1001 的服务 token 不能通过 metadata 换成 CU-1002
    forged = {"metadata": {"auth_token": token, "customer_id": "CU-1002"}}
    assert _json(order_query, {"order_no": "SO-2002"}, forged)["error"] == "not_found"
    # 不带 requested → 使用 on_behalf_of
    own = {"metadata": {"auth_token": token}}
    assert _json(order_query, {"order_no": "SO-2001"}, own)["order_no"] == "SO-2001"
    # requested 等于 on_behalf_of → 允许
    same = {"metadata": {"auth_token": token, "customer_id": "CU-1001"}}
    assert _json(order_query, {"order_no": "SO-2001"}, same)["order_no"] == "SO-2001"


# ---------------------------------------------------------------------------
# 真服务：直连 A2A + 编排 A2A
# ---------------------------------------------------------------------------

def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _spawn(target: str, port: int, env: dict):  # noqa: ANN001, ANN201
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", target, "--port", str(port),
         "--log-level", "warning"],
        env=env, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _wait_ready(port: int, token: str, timeout: float = 40) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = httpx.get(
                f"http://127.0.0.1:{port}/.well-known/agent-card.json",
                headers={"Authorization": f"Bearer {token}"}, timeout=2,
                trust_env=False,
            )
            if r.status_code == 200:
                return True
        except Exception:  # noqa: BLE001
            time.sleep(0.5)
    return False


@pytest.fixture(scope="module")
def stack():
    get_settings.cache_clear()
    seed()
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        ports = {
            n: _free_port() for n in
            ("order", "finance", "aftersale", "fulfillment", "orchestrator")
        }
        common = {
            **os.environ, "LLM_PROVIDER": "fake", "CHECKPOINTER": "sqlite",
            "ENV": "test", "AUTH_SECRET": "test-secret",
            "SHARED_DB_PATH": str(Path(tmp) / "shared.db"),
            "ORDER_PORT": str(ports["order"]),
            "FINANCE_PORT": str(ports["finance"]),
            "AFTERSALE_PORT": str(ports["aftersale"]),
            "FULFILLMENT_PORT": str(ports["fulfillment"]),
            "ORCHESTRATOR_PORT": str(ports["orchestrator"]),
        }
        targets = {
            "order": "domains.order.server:app",
            "finance": "domains.finance.server:app",
            "aftersale": "domains.aftersale.server:app",
            "fulfillment": "domains.fulfillment.server:app",
            "orchestrator": "orchestrator.server:app",
        }
        envs = {
            n: {**common, "SQLITE_DB_PATH": str(Path(tmp) / f"{n}.sqlite")}
            for n in targets
        }
        procs = {n: _spawn(t, ports[n], envs[n]) for n, t in targets.items()}
        for n, p in ports.items():
            assert _wait_ready(p, "ready-token"), f"{n}:{p} 未就绪"
        try:
            yield ports
        finally:
            for p in procs.values():
                p.terminate()
            for p in procs.values():
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    p.kill()
            seed()


def _token(actor: str, role: str = "customer") -> str:
    scopes = ROLE_SCOPES.get(role, ALL_SCOPES)
    return create_token(
        tenant_id="t_demo", actor_id=actor, role=role, scopes=scopes,
        token_type="user" if role != "service" else "service",
    )


async def _call(base: str, text: str, token: str, metadata: dict | None = None):  # noqa: ANN001
    from shared.a2a_client import delegate

    return await delegate(
        base, text, metadata=metadata, timeout=60, auth=decode_token(token),
    )


@pytest.mark.asyncio
async def test_direct_a2a_customer_cannot_read_others(stack):
    token = _token("CU-1001")
    order_url = f"http://127.0.0.1:{stack['order']}"
    r = await _call(order_url, "查询订单 SO-2002", token)
    assert r.state == TaskState.TASK_STATE_COMPLETED
    assert r.data_parts[0]["data"]["error"] == "not_found"

    own = await _call(order_url, "查询订单 SO-2001", token)
    assert own.data_parts[0]["data"]["order_no"] == "SO-2001"


@pytest.mark.asyncio
async def test_direct_a2a_customer_cannot_touch_other_domains(stack):
    token = _token("CU-1001")
    cases = [
        (stack["finance"], "为订单 SO-2002 执行退款"),
        (stack["aftersale"], "查询工单 TK-3002"),
        (stack["fulfillment"], "查询订单 SO-2002 的物流节点"),
    ]
    for port, text in cases:
        r = await _call(f"http://127.0.0.1:{port}", text, token)
        assert r.state == TaskState.TASK_STATE_COMPLETED
        assert r.data_parts[0]["data"]["error"] == "not_found", text


@pytest.mark.asyncio
async def test_orchestrator_customer_cannot_read_other_order(stack):
    token = _token("CU-1001")
    base = f"http://127.0.0.1:{stack['orchestrator']}"
    r = await _call(base, "查询订单 SO-2002 的状态", token)
    assert r.state == TaskState.TASK_STATE_COMPLETED
    final = r.data_parts[0]
    assert final["artifacts"]["order"]["data"]["error"] == "not_found"


@pytest.mark.asyncio
async def test_orchestrator_staff_requires_customer_id(stack):
    token = _token("svc-1", role="service")
    base = f"http://127.0.0.1:{stack['orchestrator']}"
    missing = await _call(base, "查询订单 SO-2002 的状态", token)
    assert missing.data_parts[0]["artifacts"]["order"]["data"]["error"] == (
        "customer_id_required"
    )
    wrong = await _call(
        base, "查询订单 SO-2002 的状态", token, {"customer_id": "CU-1001"},
    )
    assert wrong.data_parts[0]["artifacts"]["order"]["data"]["error"] == "not_found"
    own = await _call(
        base, "查询订单 SO-2001 的状态", token, {"customer_id": "CU-1001"},
    )
    assert own.data_parts[0]["artifacts"]["order"]["data"]["order_no"] == "SO-2001"


@pytest.mark.asyncio
async def test_forged_action_metadata_is_ignored(stack):
    """① 直连编排 + customer token + 伪造 metadata.action（无签名）→ 不生效。

    若 forced intent 被伪造 metadata 生效，"你好" 会被路由成 refund_settle
    并真的退掉 SO-2001；正确行为是走 smalltalk 且不产生任何退款单。
    """
    token = _token("CU-1001")
    base = f"http://127.0.0.1:{stack['orchestrator']}"
    r = await _call(
        base, "你好", token,
        {
            "action": {"intent": "refund_settle",
                       "slots": {"order_no": "SO-2001"}},
            "customer_id": "CU-1001",
        },
    )
    assert r.state == TaskState.TASK_STATE_COMPLETED
    final = r.data_parts[0] if r.data_parts else {}
    assert final.get("artifacts", {}) == {}
    assert "商家经营助手" in (r.text or "")
    conn = sqlite3.connect("infra/mockdata/mock.db")
    try:
        n = conn.execute(
            "SELECT count(*) FROM refunds WHERE tenant_id='t_demo' "
            "AND order_no='SO-2001'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert n == 0
