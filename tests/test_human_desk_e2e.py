"""转人工闭环 e2e（真服务进程，fake LLM）。

链路：客户问"转人工" → 编排 handoff 节点 A2A 委托 humandesk → 入坐席队列
→ 工作台接管 → 坐席回复 → 客户可见 → 交回 AI → AI 继续接答。
另覆盖：碰壁 2 次自动转人工、工具面板只读调用（含桩域 rejected）。
"""

import asyncio
import json as _json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import pytest
import websockets

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ["AUTH_SECRET"] = "test-secret"
os.environ["SHARED_DB_PATH"] = str(Path(tempfile.gettempdir()) / "agentdock-hd-shared.db")

from shared.auth import ROLE_SCOPES, create_token  # noqa: E402
from shared.config import get_settings  # noqa: E402


def _token(role: str, actor_id: str, tenant_id: str = "t_demo") -> str:
    return create_token(
        tenant_id=tenant_id, actor_id=actor_id, role=role,
        scopes=ROLE_SCOPES[role], token_type="user",
    )


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
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        ports = {n: _free_port() for n in
                 ("orchestrator", "merchandising", "console", "order",
                  "customer")}
        common = {
            **os.environ, "LLM_PROVIDER": "fake", "CHECKPOINTER": "sqlite",
            "DEV_MODE": "true", "AUTH_SECRET": "test-secret",
            "SHARED_DB_PATH": str(Path(tmp) / "shared.db"),
        }
        envs = {
            "merchandising": {
                **common, "SQLITE_DB_PATH": str(Path(tmp) / "merch.sqlite"),
                "MERCHANDISING_PORT": str(ports["merchandising"]),
            },
            "order": {
                **common, "SQLITE_DB_PATH": str(Path(tmp) / "order.sqlite"),
                "ORDER_PORT": str(ports["order"]),
            },
            "customer": {
                **common, "SQLITE_DB_PATH": str(Path(tmp) / "customer.sqlite"),
                "CUSTOMER_PORT": str(ports["customer"]),
            },
            "orchestrator": {
                **common, "SQLITE_DB_PATH": str(Path(tmp) / "orch.sqlite"),
                "ORCHESTRATOR_PORT": str(ports["orchestrator"]),
                "MERCHANDISING_PORT": str(ports["merchandising"]),
                "CONSOLE_PORT": str(ports["console"]),
                # 域地址必须指向本夹具起的真域，否则会落到开发机上默认 8302
                "ORDER_PORT": str(ports["order"]),
                "CUSTOMER_PORT": str(ports["customer"]),
            },
            # console 的工具面板要直连域 Server，端口必须与实际一致
            "console": {
                **common, "SQLITE_DB_PATH": str(Path(tmp) / "humandesk.sqlite"),
                # 审计库（console/db.py 的 DB_PATH）独立隔离，verify/篡改测试才可靠
                "CONSOLE_DB_PATH": str(Path(tmp) / "audit-chain.sqlite"),
                "CONSOLE_PORT": str(ports["console"]),
                "ORCHESTRATOR_PORT": str(ports["orchestrator"]),
                "MERCHANDISING_PORT": str(ports["merchandising"]),
                "ORDER_PORT": str(ports["order"]),
                "CUSTOMER_PORT": str(ports["customer"]),
            },
        }
        targets = {
            "merchandising": "domains.merchandising.server:app",
            "order": "domains.order.server:app",
            "customer": "domains.customer.server:app",
            "orchestrator": "orchestrator.server:app",
            "console": "console.server:app",
        }
        procs = {n: _spawn(targets[n], ports[n], envs[n])
                 for n in ("merchandising", "order", "customer",
                           "orchestrator", "console")}
        for n, p in ports.items():
            assert _wait_ready(p, "test-token"), f"{n} :{p} 未就绪"
        yield {"ports": ports, "procs": procs,
               "console_db": str(Path(tmp) / "audit-chain.sqlite"),
               "shared_db": str(Path(tmp) / "shared.db")}
        for p in procs.values():
            p.terminate()
        for p in procs.values():
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()


@pytest.fixture()
def client(stack):
    c = httpx.Client(
        base_url=f"http://127.0.0.1:{stack['ports']['console']}",
        headers={"Authorization": f"Bearer {_token('agent', 'AG-001')}"},
        timeout=60, trust_env=False,
    )
    yield c


@pytest.fixture()
def customer_client(stack):
    c = httpx.Client(
        base_url=f"http://127.0.0.1:{stack['ports']['console']}",
        headers={"Authorization": f"Bearer {_token('customer', 'CU-1001')}"},
        timeout=60, trust_env=False,
    )
    yield c


@pytest.fixture()
def supervisor_client(stack):
    c = httpx.Client(
        base_url=f"http://127.0.0.1:{stack['ports']['console']}",
        headers={"Authorization": f"Bearer {_token('supervisor', 'SUP-001')}"},
        timeout=60, trust_env=False,
    )
    yield c


@pytest.fixture()
def client_with_db(client, supervisor_client, stack):
    """agent + supervisor + 统一审计库文件路径（篡改检测用）。"""
    return client, supervisor_client, stack["shared_db"]


def _send(client: httpx.Client, text: str, conv_id: str | None = None,
          customer_id: str | None = None) -> dict:
    body = {"conversation_id": conv_id, "text": text}
    if customer_id:
        body["customer_id"] = customer_id
    r = client.post("/api/customer/send", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_login_issues_jwt_and_me(stack):
    base = f"http://127.0.0.1:{stack['ports']['console']}"
    r = httpx.post(
        f"{base}/api/auth/login",
        json={"tenant_id": "t_demo", "username": "agent", "password": "demo123"},
        timeout=30, trust_env=False,
    )
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    me = httpx.get(
        f"{base}/api/auth/me",
        headers={"Authorization": f"Bearer {token}"}, timeout=30, trust_env=False,
    )
    assert me.status_code == 200
    assert me.json()["role"] == "agent"
    assert me.json()["tenant_id"] == "t_demo"

    bad = httpx.post(
        f"{base}/api/auth/login",
        json={"tenant_id": "t_demo", "username": "agent", "password": "wrong"},
        timeout=30, trust_env=False,
    )
    assert bad.status_code == 401
    no_token = httpx.get(f"{base}/api/conversations", timeout=30, trust_env=False)
    assert no_token.status_code == 401


async def _recv_until(ws, pred, timeout: float = 30) -> dict:  # noqa: ANN001
    """收事件直到谓词命中（跳过 status 等中间事件）。"""
    while True:
        evt = _json.loads(await asyncio.wait_for(ws.recv(), timeout))
        if pred(evt):
            return evt


async def _ws_auth(ws, token: str) -> dict:  # noqa: ANN001
    """WebSocket 首包认证，返回 snapshot。"""
    await ws.send(_json.dumps({"type": "auth", "token": token}))
    ack = _json.loads(await asyncio.wait_for(ws.recv(), 10))
    assert ack["type"] == "auth_ok", ack
    snap = _json.loads(await asyncio.wait_for(ws.recv(), 10))
    assert snap["type"] == "snapshot", snap
    return snap


@pytest.mark.asyncio
async def test_websocket_bidirectional_chat(stack, client):
    """WS 双向聊天：客户/坐席同会话互发实时可见；REST 状态变更也扇出到两端。"""
    r = _send(client, "查询 SKU-A001 的商品详情", customer_id="CU-1001")
    cid = r["conversation_id"]
    uri = f"ws://127.0.0.1:{stack['ports']['console']}/ws/{cid}"

    async with websockets.connect(uri) as cust, websockets.connect(uri) as agent:
        snap = await _ws_auth(cust, _token("customer", "CU-1001"))
        assert snap["role"] == "customer"
        snap_a = await _ws_auth(agent, _token("agent", "AG-001"))
        assert snap_a["role"] == "agent"

        # 客户发消息（WS）→ 两端都实时收到客户消息，随后 AI 澄清
        await cust.send(_json.dumps({"text": "这个鼠标保修多久"}))
        evt_a = await _recv_until(agent, lambda e: e.get("type") == "message"
                                  and e["message"]["role"] == "customer")
        assert "保修" in evt_a["message"]["content"]
        await _recv_until(cust, lambda e: e.get("type") == "message"
                          and e["message"]["role"] == "ai")

        # 客户要求转人工 → 双端实时看到会话转 pending_handoff
        await cust.send(_json.dumps({"text": "帮我转人工"}))
        evt = await _recv_until(cust, lambda e: e.get("type") == "message"
                                and "转接人工" in e["message"]["content"])
        assert evt["conversation"]["status"] == "pending_handoff"

        # REST 接管 → 状态变更扇出到两端
        resp = client.post(f"/api/conversations/{cid}/takeover")
        assert resp.status_code == 200
        evt = await _recv_until(agent, lambda e: e.get("type") == "message"
                                and "人工客服已接入" in e["message"]["content"])
        assert evt["conversation"]["mode"] == "human"

        # 坐席经 WS 回复 → 客户实时收到
        await agent.send(_json.dumps({"text": "您好，人工坐席在线，整机保修一年。"}))
        evt = await _recv_until(cust, lambda e: e.get("type") == "message"
                                and e["message"]["role"] == "agent")
        assert "保修一年" in evt["message"]["content"]


@pytest.mark.asyncio
async def test_websocket_auth_rejected(stack):
    """WS 鉴权：首包 token 错误直接关闭。"""
    r = httpx.post(
        f"http://127.0.0.1:{stack['ports']['console']}/api/customer/send",
        headers={"Authorization": f"Bearer {_token('customer', 'CU-1001')}",
                 "Content-Type": "application/json"},
        json={"text": "你好"}, timeout=30, trust_env=False,
    )
    cid = r.json()["conversation_id"]
    uri = f"ws://127.0.0.1:{stack['ports']['console']}/ws/{cid}"
    with pytest.raises(Exception):  # noqa: B017, PT011  (close/1008 均算拒绝)
        async with websockets.connect(uri) as ws:
            await ws.send(_json.dumps({"type": "auth", "token": "WRONG"}))
            await asyncio.wait_for(ws.recv(), 10)


def test_request_human_full_loop(client, stack):
    """明确要求转人工 → 入队 → 接管 → 回复 → 客户可见 → 交回 AI。"""
    # 1) 客户要求转人工 → 编排 handoff 节点委托 humandesk → 待接管
    r = _send(client, "帮我转人工")
    cid = r["conversation_id"]
    assert "转接人工" in r["reply"]
    conv = next(c for c in client.get("/api/conversations").json() if c["id"] == cid)
    assert conv["status"] == "pending_handoff"

    # 2) 交接包已入队（结构化，含 contextId/触发原因/AI 草案）
    h = client.get(f"/api/conversations/{cid}/handoff").json()
    assert h["status"] == "pending"
    assert "转人工" in h["package"]["context_id"] or h["package"]["context_id"] == cid
    assert any("转人工" in x for x in h["package"]["reasons"])

    # 3) 坐席接管 → 人工模式 → 系统提示
    r = client.post(f"/api/conversations/{cid}/takeover")
    assert r.status_code == 200
    assert r.json()["status"] == "with_human"
    msgs = client.get(f"/api/conversations/{cid}/messages").json()
    assert any("人工客服已接入" in m["content"] for m in msgs)

    # 4) 坐席回复 → 落库（客户页 SSE 可见）
    r = client.post(f"/api/conversations/{cid}/reply", json={"text": "您好，我是人工坐席小李，有什么可以帮您？"})
    assert r.status_code == 200
    msgs = client.get(f"/api/conversations/{cid}/messages").json()
    assert any("人工坐席小李" in m["content"] for m in msgs)

    # 5) 交回 AI → 客户继续问商品，AI 正常接答
    r = client.post(f"/api/conversations/{cid}/return")
    assert r.json()["status"] == "active"
    r = _send(client, "查询 SKU-A001 的商品详情", cid)
    assert "无线鼠标" in r["reply"]


def test_customer_cannot_use_workbench_tools(customer_client):
    """客户 token 不能调坐席工具面板/内部工具（服务端强制，不靠前端隐藏）。"""
    assert customer_client.get("/api/tools").status_code == 403
    r = customer_client.post(
        "/api/tools/customer.query/invoke", json={"params": {"query": "王小明"}}
    )
    assert r.status_code == 403
    # 客户只能看到自己的会话（不会列出同租户其他客户的会话）
    for c in customer_client.get("/api/conversations").json():
        assert c.get("customer_id") in (None, "CU-1001")


def test_two_walls_auto_handoff(client):
    """碰壁 2 次：两次澄清后仍 nomatch → 第二次当场转人工。"""
    r = _send(client, "帮我处理一下那个东西")           # 澄清
    cid = r["conversation_id"]
    assert r["type"] == "clarify"
    r = _send(client, "都不是", cid)                    # → nomatch #1（wall=1）
    assert "超出" in r["reply"]

    r = _send(client, "算了不管了就这样吧", cid)        # 澄清
    assert r["type"] == "clarify"
    r = _send(client, "都不是", cid)                    # → nomatch #2（wall=2）→ handoff
    assert "转接人工" in r["reply"]
    conv = client.get("/api/conversations").json()
    mine = next(c for c in conv if c["id"] == cid)
    assert mine["status"] == "pending_handoff"
    assert mine["wall_count"] >= 2


def test_tool_panel_readonly(client, supervisor_client):
    """右侧工具面板：坐席专属查用户 OK；商品域 OK；订单域 OK（真实现）。"""
    r = client.post("/api/tools/customer.query/invoke",
                    json={"params": {"query": "王小明"}}).json()
    assert r["state"] == "TASK_STATE_COMPLETED"
    # customer.query 已升格为 customer 域工具：data 是完整 Artifact 信封，
    # 业务结果在信封的 data 字段（与其它 A2A 工具一致）
    assert r["data"]["data"][0]["name"] == "王小明"
    assert r["data"]["data"][0]["phone"].startswith("138****")  # 脱敏入库

    r = client.post("/api/tools/product.lookup/invoke",
                    json={"params": {"sku": "SKU-A001"}}).json()
    assert r["state"] == "TASK_STATE_COMPLETED"
    assert r["data"]["data"]["name"] == "无线鼠标 Pro"

    r = client.post("/api/tools/order.query/invoke",
                    json={"params": {"order_no": "SO-2001", "customer_id": "CU-1001"}}).json()
    assert r["state"] == "TASK_STATE_COMPLETED"
    assert r["data"]["data"]["order_no"] == "SO-2001"

    # 再走一次主图链路，覆盖裁决/交付审计
    r = _send(client, "查询 SKU-A001 的商品详情")
    assert "无线鼠标" in r["reply"]

    audit = supervisor_client.get("/api/audit").json()
    assert len(audit) >= 3  # 全部留痕（PRD 8.3）

    # 哈希链校验通过（AC-8）
    v = supervisor_client.get("/api/audit/verify").json()
    assert v["ok"] is True and v["total"] >= 3 and v["broken_at"] is None

    # 统一审计覆盖：SendMessage / Task 状态跃迁 / 工具调用 / 裁决 / 交付
    # （customer.query 升格为域工具后走 A2A 代理，审计事件为 console_dispatch）
    events = supervisor_client.get("/api/audit/events").json()
    actions = {e["action"] for e in events}
    assert {"a2a.send", "task.submitted", "task.completed",
            "tool.call", "tool.console_dispatch", "arbitrate.synthesize",
            "deliver.answer"} <= actions


def test_cookie_session_and_csrf(stack):
    base = f"http://127.0.0.1:{stack['ports']['console']}"
    with httpx.Client(base_url=base, trust_env=False, timeout=30) as c:
        r = c.post("/api/auth/login", json={
            "tenant_id": "t_demo", "username": "customer",
            "password": "demo123", "surface": "customer",
        })
        assert r.status_code == 200, r.text
        assert "agentdock_customer_session" in r.cookies
        assert "agentdock_customer_csrf" in r.cookies
        assert "HttpOnly" in r.headers.get("set-cookie", "")
        csrf = r.json()["csrf_token"]
        me = c.get("/api/auth/me", headers={"X-Surface": "customer"})
        assert me.status_code == 200
        assert me.json()["csrf_token"] == csrf

        bad = c.post("/api/customer/send", headers={"X-Surface": "customer"},
                     json={"text": "你好"})
        assert bad.status_code == 403 and bad.json()["error"] == "csrf_invalid"
        good = c.post(
            "/api/customer/send",
            headers={"X-Surface": "customer", "X-CSRF-Token": csrf},
            json={"text": "你好"},
        )
        assert good.status_code == 200, good.text

        out = c.post("/api/auth/logout", headers={
            "X-Surface": "customer", "X-CSRF-Token": csrf,
        })
        assert out.status_code == 200
        assert c.get("/api/auth/me", headers={"X-Surface": "customer"}).status_code == 401


def test_workbench_cookie_is_separate(stack):
    base = f"http://127.0.0.1:{stack['ports']['console']}"
    with httpx.Client(base_url=base, trust_env=False, timeout=30) as c:
        r = c.post("/api/auth/login", json={
            "tenant_id": "t_demo", "username": "agent",
            "password": "demo123", "surface": "workbench",
        })
        assert r.status_code == 200, r.text
        assert "agentdock_workbench_session" in r.cookies
        assert "agentdock_customer_session" not in r.cookies
        assert c.get(
            "/api/conversations", headers={"X-Surface": "workbench"},
        ).status_code == 200


@pytest.mark.asyncio
async def test_ws_rejects_bad_origin(customer_client, stack):
    r = _send(customer_client, "你好")
    cid = r["conversation_id"]
    uri = f"ws://127.0.0.1:{stack['ports']['console']}/ws/{cid}?surface=customer"
    with pytest.raises(Exception):  # noqa: B017, PT011
        async with websockets.connect(uri, origin="http://evil.example") as ws:
            await asyncio.wait_for(ws.recv(), 5)


def test_customer_handoff_dto_hides_internal_fields(customer_client):
    r = _send(customer_client, "帮我转人工")
    cid = r["conversation_id"]
    h = customer_client.get(f"/api/conversations/{cid}/handoff").json()
    assert h["status"] == "pending"
    assert h["reasons"]
    assert "package" not in h
    assert "ai_draft" not in h and "decision_log" not in h


def test_agent_assist_ask_and_history(client):
    """toB 坐席助手：坐席直接问主 Agent（编排零改动，同一条主图链路）+ 留痕。"""
    r = client.post("/api/assist/ask", json={"question": "查询 SKU-A001 的商品详情"})
    assert r.status_code == 200
    body = r.json()
    assert body["type"] == "ai"
    assert "无线鼠标" in body["reply"]
    hist = client.get("/api/assist/history").json()
    assert [m["role"] for m in hist[-2:]] == ["agent", "ai"]


def test_agent_assist_clarify_resume(client):
    """toB 助手澄清续问：模糊问题 → clarify（带 task_id）→ 回填 → completed。"""
    r1 = client.post("/api/assist/ask", json={"question": "帮我处理一下那个东西"})
    body1 = r1.json()
    assert body1["type"] == "clarify"
    assert body1["task_id"]

    r2 = client.post("/api/assist/ask", json={
        "question": "查询 SKU-A002 的到手价", "task_id": body1["task_id"],
    })
    body2 = r2.json()
    assert body2["type"] == "ai"
    assert "429.1" in body2["reply"]
    assert body2["task_id"] is None


def test_customer_identity_in_handoff(client):
    """客户身份贯通：customer_id → 会话绑定 → 交接包必带（PRD 8.2 行级依据）。"""
    r = client.post("/api/customer/send",
                    json={"customer_id": "CU-1002", "text": "帮我转人工"}).json()
    cid = r["conversation_id"]
    assert "转接人工" in r["reply"]
    conv = next(x for x in client.get("/api/conversations").json() if x["id"] == cid)
    assert conv["customer_id"] == "CU-1002"
    assert conv["customer_name"] == "李商户"  # mock 库反查
    h = client.get(f"/api/conversations/{cid}/handoff").json()
    assert h["package"]["customer_id"] == "CU-1002"


def test_order_query_via_orchestrator(client):
    """订单一类意图（M2 真实现）：路由到订单域，返回真实订单而非"暂不可用"。"""
    r = client.post("/api/customer/send",
                    json={"customer_id": "CU-1001", "text": "帮我查一下我的订单"}).json()
    reply = r["reply"]
    assert "暂不可用" not in reply
    assert "超出了我目前能处理的范围" not in reply
    # 会话客户 CU-1001 的订单 SO-2001（已签收）应出现在产物里
    assert "SO-2001" in reply
    assert "signed" in reply or "已签收" in reply


def test_audit_chain_tamper_detected(client_with_db):
    """绕过接口直接改库 → verify 精确定位被篡改行（哈希链防内部篡改，8.3）。"""
    agent, supervisor, db_path = client_with_db
    r = agent.post("/api/tools/customer.query/invoke",
                   json={"params": {"query": "王小明"}}).json()
    assert r["state"] == "TASK_STATE_COMPLETED"
    v = supervisor.get("/api/audit/verify").json()
    assert v["ok"] is True

    # 测试进程直连同一 sqlite 文件改内容（模拟拿到库权限的内部篡改）
    import sqlite3

    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute(
            "UPDATE audit_events SET result_summary='洗掉记录' "
            "WHERE id=(SELECT MAX(id) FROM audit_events)")
    conn.close()
    v2 = supervisor.get("/api/audit/verify").json()
    assert v2["ok"] is False and v2["broken_at"] is not None
    assert "被篡改" in v2["reason"]
