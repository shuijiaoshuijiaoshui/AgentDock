"""M3 串行链 e2e（真服务进程，fake LLM）：分层调度、上游否决、Saga 补偿。

链路（PRD 7.9 / 7.7）：
- refund_status：售后资格 → 履约退货状态 → 财务退款单（三跳串行）
- cancel_refund：订单取消（写）→ 财务退款（写）；财务失败 → 逆序补偿恢复订单
- order_amend：履约状态判断 → 订单改地址（已发货被否决）
"""

import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ["AUTH_SECRET"] = "test-secret"
os.environ["SHARED_DB_PATH"] = str(Path(tempfile.gettempdir()) / "agentdock-m3-shared.db")

from a2a.client import ClientConfig, ClientFactory  # noqa: E402
from a2a.types import TaskState  # noqa: E402
from a2a.types.a2a_pb2 import SendMessageRequest  # noqa: E402

import a2a.helpers as ah  # noqa: E402
from shared.auth import ALL_SCOPES, create_token  # noqa: E402
from shared.config import get_settings  # noqa: E402


def _token() -> str:
    return create_token(
        tenant_id="t_demo", actor_id="test-service", role="service",
        scopes=ALL_SCOPES, token_type="service",
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
        env=env, cwd=ROOT,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _wait_ready(port: int, token: str, timeout: float = 40) -> bool:
    import httpx

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


SERVICES = ("orchestrator", "aftersale", "fulfillment", "finance", "order",
           "merchandising")


@pytest.fixture(autouse=True)
def _fresh_mockdb():
    """Saga/改地址用例会写 mock.db：每个用例前后都重置回种子态，互不污染。"""
    from scripts.seed_mock import seed

    seed()
    yield
    seed()


@pytest.fixture(scope="module")
def stack():
    get_settings.cache_clear()
    # 用例会写 mock.db（取消订单/退款单），结束统一重置回种子态
    from scripts.seed_mock import seed

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        ports = {n: _free_port() for n in SERVICES}
        common = {
            **os.environ, "LLM_PROVIDER": "fake", "CHECKPOINTER": "sqlite",
            "DEV_MODE": "true", "AUTH_SECRET": "test-secret",
            "SHARED_DB_PATH": str(Path(tmp) / "shared.db"),
        }
        port_keys = {
            "orchestrator": "ORCHESTRATOR_PORT",
            "aftersale": "AFTERSALE_PORT",
            "fulfillment": "FULFILLMENT_PORT",
            "finance": "FINANCE_PORT",
            "order": "ORDER_PORT",
            "merchandising": "MERCHANDISING_PORT",
        }
        targets = {
            "orchestrator": "orchestrator.server:app",
            "aftersale": "domains.aftersale.server:app",
            "fulfillment": "domains.fulfillment.server:app",
            "finance": "domains.finance.server:app",
            "order": "domains.order.server:app",
            "merchandising": "domains.merchandising.server:app",
        }
        envs = {}
        for n in SERVICES:
            env = {**common,
                   "SQLITE_DB_PATH": str(Path(tmp) / f"{n}.sqlite"),
                   port_keys[n]: str(ports[n])}
            if n == "orchestrator":
                # 域地址必须指向本夹具的进程，否则会落到开发机默认端口
                env.update({
                    "AFTERSALE_PORT": str(ports["aftersale"]),
                    "FULFILLMENT_PORT": str(ports["fulfillment"]),
                    "FINANCE_PORT": str(ports["finance"]),
                    "ORDER_PORT": str(ports["order"]),
                    "MERCHANDISING_PORT": str(ports["merchandising"]),
                })
            envs[n] = env
        procs = {n: _spawn(targets[n], ports[n], envs[n]) for n in SERVICES}
        for n in SERVICES:
            assert _wait_ready(ports[n], "test-token"), f"{n} :{ports[n]} 未就绪"
        yield {"ports": ports}
        for p in procs.values():
            p.terminate()
        for p in procs.values():
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
    seed()  # 还原 mock.db 种子态（取消/退款等写操作回滚）


def _client(port: int, token: str | None = None):  # noqa: ANN001, ANN201
    import httpx

    hc = httpx.AsyncClient(
        timeout=90, headers={"Authorization": f"Bearer {token or _token()}"},
        trust_env=False,
    )
    factory = ClientFactory(ClientConfig(httpx_client=hc))
    return factory, hc


async def _send_and_collect(client, text: str,
                            metadata: dict | None = None) -> dict:  # noqa: ANN001
    req = SendMessageRequest(message=ah.new_text_message(text))
    if metadata:
        req.metadata.update(metadata)
    out = {"state": None, "data_parts": [], "text": ""}
    async for resp in client.send_message(req):
        kind = resp.WhichOneof("payload")
        if kind == "status_update":
            out["state"] = resp.status_update.status.state
        elif kind == "artifact_update":
            out["data_parts"].extend(ah.get_data_parts(resp.artifact_update.artifact.parts))
            texts = ah.get_text_parts(resp.artifact_update.artifact.parts)
            if texts:
                out["text"] = texts[-1]
    return out


def _order_status(order_no: str) -> str:
    conn = sqlite3.connect("infra/mockdata/mock.db")
    try:
        return conn.execute(
            "SELECT status FROM orders WHERE tenant_id='t_demo' AND order_no=?",
            (order_no,),
        ).fetchone()[0]
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_refund_status_serial_chain_green(stack):
    """三跳串行全绿：资格(售后)→入库(履约)→退款单(财务)，金额只认财务。"""
    port = stack["ports"]["orchestrator"]
    factory, hc = _client(port)
    client = await factory.create_from_url(f"http://127.0.0.1:{port}/")
    try:
        r = await _send_and_collect(
            client, "SKU-A003 的退款，SO-2003 退到哪了",
            metadata={"customer_id": "CU-1003"})
        assert r["state"] == TaskState.TASK_STATE_COMPLETED
        final = r["data_parts"][0]
        arts = final["artifacts"]
        # 三域全部有产物（串行逐跳，不是并行扇出）
        assert set(arts) == {"aftersale", "fulfillment", "finance"}
        assert arts["aftersale"]["data"]["eligible"] is True
        # 金额权威=财务（3.4）：退款单 2199
        assert arts["finance"]["data"][0]["amount"] == 2199.0
        # 裁决留痕在 decision_log
        events = [e.get("event") for e in final["decision_log"]]
        assert "no_conflict" in events
        assert "SO-2003" in r["text"]
    finally:
        await hc.aclose()


@pytest.mark.asyncio
async def test_refund_veto_cancels_downstream(stack):
    """上游否决（7.9 第 4 步）：资格不通过 → 履约/财务被取消，不调用。"""
    port = stack["ports"]["orchestrator"]
    factory, hc = _client(port)
    client = await factory.create_from_url(f"http://127.0.0.1:{port}/")
    try:
        r = await _send_and_collect(
            client, "SKU-A002 的退款，SO-2005 退到哪了",
            metadata={"customer_id": "CU-1004"})
        assert r["state"] == TaskState.TASK_STATE_COMPLETED
        final = r["data_parts"][0]
        arts = final["artifacts"]
        # 资格不通过：eligible=False（SKU-A002 超 12 个月）
        assert arts["aftersale"]["data"]["eligible"] is False
        # 下游被取消（有取消标记而非真实数据）
        assert arts["fulfillment"]["data"]["error"] == "cancelled_by_upstream"
        assert arts["finance"]["data"]["error"] == "cancelled_by_upstream"
        # 作答里说清"谁被取消、为什么"
        assert "已取消" in r["text"] and "资格判定不通过" in r["text"]
        veto = [e for e in final["decision_log"] if e.get("event") == "veto"]
        assert veto and set(veto[0]["cancelled"]) == {"fulfillment", "finance"}
    finally:
        await hc.aclose()


@pytest.mark.asyncio
async def test_cancel_refund_requires_refund_approval(stack):
    """取消退款链：订单先取消；退款金额无论多少都必须人工审批，未批不落退款单。"""
    port = stack["ports"]["orchestrator"]
    factory, hc = _client(port)
    client = await factory.create_from_url(f"http://127.0.0.1:{port}/")
    try:
        r = await _send_and_collect(
            client, "取消订单 SO-2001 给我退款",
            metadata={"customer_id": "CU-1001"})
        assert r["state"] == TaskState.TASK_STATE_COMPLETED
        final = r["data_parts"][0]
        arts = final["artifacts"]
        assert arts["order"]["data"]["status"] == "cancelled"
        # 退款不再自动执行：返回待审批单
        assert arts["finance"]["data"]["status"] == "pending_approval"
        assert arts["finance"]["data"]["approval_id"]
        assert _order_status("SO-2001") == "cancelled"
        conn = sqlite3.connect("infra/mockdata/mock.db")
        try:
            n = conn.execute(
                "SELECT count(*) FROM refunds WHERE tenant_id='t_demo' "
                "AND order_no='SO-2001'").fetchone()[0]
        finally:
            conn.close()
        assert n == 0, "未审批前不得落退款单"
    finally:
        await hc.aclose()


@pytest.mark.asyncio
async def test_cancel_refund_saga_compensation(stack):
    """Saga 补偿：chaos 注入财务故障 → 逆序恢复订单，无悬挂状态（PRD 7.7）。"""
    port = stack["ports"]["orchestrator"]
    factory, hc = _client(port)
    client = await factory.create_from_url(f"http://127.0.0.1:{port}/")
    try:
        r = await _send_and_collect(
            client, "取消订单 SO-2001 给我退款",
            metadata={"chaos": "finance_settle_fail", "customer_id": "CU-1001"})
        assert r["state"] == TaskState.TASK_STATE_COMPLETED
        final = r["data_parts"][0]
        # 正向：订单已取消；财务：故障
        assert final["artifacts"]["order"]["data"]["status"] == "cancelled"
        assert final["artifacts"]["finance"]["data"]["error"] == "chaos_injected"
        # 补偿留痕 + 回滚成功口径
        saga = [e for e in final["decision_log"] if e.get("stage") == "saga_compensate"]
        assert saga and saga[0]["ok"] is True and saga[0]["skill"] == "order.restore"
        assert "已全部回滚" in r["text"]
        # 数据库无悬挂：订单恢复原状态、未落退款单
        assert _order_status("SO-2001") == "signed"
        conn = sqlite3.connect("infra/mockdata/mock.db")
        try:
            n = conn.execute(
                "SELECT count(*) FROM refunds WHERE tenant_id='t_demo' "
                "AND order_no='SO-2001'").fetchone()[0]
            assert n == 0
        finally:
            conn.close()
    finally:
        await hc.aclose()


@pytest.mark.asyncio
async def test_order_amend_veto_shipped(stack):
    """改地址串行链：已签收订单被履约域否决，订单域不被调用。"""
    port = stack["ports"]["orchestrator"]
    factory, hc = _client(port)
    client = await factory.create_from_url(f"http://127.0.0.1:{port}/")
    try:
        r = await _send_and_collect(
            client, "帮我把 SO-2001 改地址",
            metadata={"customer_id": "CU-1001"})
        assert r["state"] == TaskState.TASK_STATE_COMPLETED
        final = r["data_parts"][0]
        arts = final["artifacts"]
        assert arts["fulfillment"]["data"]["status"] == "delivered"
        assert arts["order"]["data"]["error"] == "cancelled_by_upstream"
        assert "已发货" in r["text"]
        assert _order_status("SO-2001") == "signed"  # 地址未动、状态未动
    finally:
        await hc.aclose()


@pytest.mark.asyncio
async def test_promo_arbitrate_finance_is_authority(stack):
    """跨域仲裁（7.6）：订单→商品(依赖注入 sku)∥财务 分层 DAG；实付以财务为准。"""
    port = stack["ports"]["orchestrator"]
    factory, hc = _client(port)
    client = await factory.create_from_url(f"http://127.0.0.1:{port}/")
    try:
        r = await _send_and_collect(
            client, "SO-2001 实付多少钱",
            metadata={"customer_id": "CU-1001"})
        assert r["state"] == TaskState.TASK_STATE_COMPLETED
        final = r["data_parts"][0]
        arts = final["artifacts"]
        # 三域产物齐：订单(sku 源) + 商品(估算) + 财务(权威)
        assert {"order", "merchandising", "finance"} <= set(arts)
        assert arts["merchandising"]["data"]["estimate"] is True
        # 裁决留痕：财务赢，营销估算被丢弃
        arb = [e for e in final["decision_log"] if e.get("stage") == "arbitrate_value"]
        assert arb and arb[0]["authority"] == "finance" and arb[0]["value"] == 199.0
        assert arb[0]["discarded"][0]["domain"] == "merchandising"
        # 口径：实付为准 + 估算仅参考
        assert "以财务账单为准" in r["text"] and "参考" in r["text"]
    finally:
        await hc.aclose()


@pytest.mark.asyncio
async def test_promo_arbitrate_order_failure_cancels_estimate_only(stack):
    """订单域失败：商品域（依赖 sku）被取消，财务域（无依赖）照常应答——
    单点失败只影响依赖它的分支，不拖垮整链。"""
    port = stack["ports"]["orchestrator"]
    factory, hc = _client(port)
    client = await factory.create_from_url(f"http://127.0.0.1:{port}/")
    try:
        r = await _send_and_collect(
            client, "SO-9999 实付多少钱",
            metadata={"customer_id": "CU-1001"})
        assert r["state"] == TaskState.TASK_STATE_COMPLETED
        final = r["data_parts"][0]
        arts = final["artifacts"]
        assert arts["order"]["data"]["error"] == "not_found"
        assert arts["merchandising"]["data"]["error"] == "cancelled_by_upstream"
        # 财务也无账单（订单不存在）→ 权威无数据，不得用估算补位（估算域已被取消）
        assert "暂不可查" in r["text"] or "暂不可用" in r["text"]
    finally:
        await hc.aclose()
