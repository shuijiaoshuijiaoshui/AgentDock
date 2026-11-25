"""端到端冒烟（真服务进程，fake LLM 模式）。

覆盖：编排→商品域全链路、澄清 interrupt→resume、重启恢复（AC-14）、订单域真实现。
"""

import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 测试服务的密钥与 .env 不同：先改环境再清 settings 缓存
os.environ["AUTH_SECRET"] = "test-secret"
os.environ["SHARED_DB_PATH"] = str(Path(tempfile.gettempdir()) / "agentdock-shared-test.db")

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
    get_settings.cache_clear()  # 让测试 env 生效
    # Windows：进程退出后 sqlite 句柄短暂滞留，忽略临时目录清理报错
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        ports = {
            "orchestrator": _free_port(),
            "merchandising": _free_port(),
            "order": _free_port(),
        }
        common = {
            **os.environ,
            "LLM_PROVIDER": "fake",
            "CHECKPOINTER": "sqlite",
            "DEV_MODE": "true",
            "AUTH_SECRET": "test-secret",
            "SHARED_DB_PATH": str(Path(tmp) / "shared.db"),
        }
        envs = {
            "merchandising": {
                **common,
                "SQLITE_DB_PATH": str(Path(tmp) / "merch.sqlite"),
                # Card 里广播的 url 由 settings 端口拼出，必须与实际端口一致，
                # 否则 a2a client 会跟随 Card 打到别的实例
                "MERCHANDISING_PORT": str(ports["merchandising"]),
            },
            "order": {
                **common,
                "SQLITE_DB_PATH": str(Path(tmp) / "order.sqlite"),
                "ORDER_PORT": str(ports["order"]),
            },
            "orchestrator": {
                **common,
                "SQLITE_DB_PATH": str(Path(tmp) / "orch.sqlite"),
                "ORCHESTRATOR_PORT": str(ports["orchestrator"]),
                "MERCHANDISING_PORT": str(ports["merchandising"]),
                "ORDER_PORT": str(ports["order"]),
            },
        }
        targets = {
            "merchandising": "domains.merchandising.server:app",
            "order": "domains.order.server:app",
            "orchestrator": "orchestrator.server:app",
        }
        procs: dict[str, subprocess.Popen] = {}
        for name in ("merchandising", "order", "orchestrator"):
            procs[name] = _spawn(targets[name], ports[name], envs[name])
        for name, port in ports.items():
            assert _wait_ready(port, "test-token"), f"{name} :{port} 未就绪"

        yield {"ports": ports, "envs": envs, "targets": targets, "procs": procs}

        for p in procs.values():
            p.terminate()
        for p in procs.values():
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()


def _client(port: int, token: str | None = None):  # noqa: ANN001, ANN201
    hc = httpx.AsyncClient(
        timeout=60, headers={"Authorization": f"Bearer {token or _token()}"},
        trust_env=False,
    )
    factory = ClientFactory(ClientConfig(httpx_client=hc))
    return factory, hc


async def _send_and_collect(client, text: str,
                            metadata: dict | None = None) -> dict:  # noqa: ANN001
    req = SendMessageRequest(message=ah.new_text_message(text))
    if metadata:
        req.metadata.update(metadata)
    out = {"state": None, "task_id": "", "context_id": "", "data_parts": [],
           "text": "", "status_data": None}
    async for resp in client.send_message(req):
        kind = resp.WhichOneof("payload")
        if kind == "task":
            out["task_id"], out["context_id"] = resp.task.id, resp.task.context_id
            out["state"] = resp.task.status.state
        elif kind == "status_update":
            ev = resp.status_update
            out["state"] = ev.status.state
            out["task_id"], out["context_id"] = ev.task_id, ev.context_id
            if ev.status.message:
                parts = ah.get_data_parts(ev.status.message.parts)
                if parts:
                    out["status_data"] = parts[-1]
        elif kind == "artifact_update":
            out["data_parts"].extend(ah.get_data_parts(resp.artifact_update.artifact.parts))
            texts = ah.get_text_parts(resp.artifact_update.artifact.parts)
            if texts:
                out["text"] = texts[-1]
    return out


@pytest.mark.asyncio
async def test_merchandising_direct(stack):
    factory, hc = _client(stack["ports"]["merchandising"])
    client = await factory.create_from_url(
        f"http://127.0.0.1:{stack['ports']['merchandising']}/"
    )
    try:
        r = await _send_and_collect(
            client, "查询 SKU-A001 的商品详情",
            metadata={"scope": "merchandising.read"},
        )
        assert r["state"] == TaskState.TASK_STATE_COMPLETED
        artifact = r["data_parts"][0]
        assert artifact["authority_domain"] == "merchandising"
        assert artifact["snapshot_id"]
        assert artifact["data"]["name"] == "无线鼠标 Pro"
    finally:
        await hc.aclose()


@pytest.mark.asyncio
async def test_orchestrator_full_chain(stack):
    factory, hc = _client(stack["ports"]["orchestrator"])
    client = await factory.create_from_url(
        f"http://127.0.0.1:{stack['ports']['orchestrator']}/"
    )
    try:
        r = await _send_and_collect(client, "查询 SKU-A001 的商品详情")
        assert r["state"] == TaskState.TASK_STATE_COMPLETED
        final = r["data_parts"][0]
        assert final["type"] == "final_answer"
        assert final["artifacts"]["merchandising"]["authority_domain"] == "merchandising"
        assert "无线鼠标" in r["text"]
    finally:
        await hc.aclose()


@pytest.mark.asyncio
async def test_order_domain_direct(stack):
    """订单域真实现：Card 可发现 + order.query 返回真实订单（不再是桩）。"""
    from shared.a2a_client import delegate

    r = await delegate(
        f"http://127.0.0.1:{stack['ports']['order']}", "查询订单 SO-2001",
        metadata={"scope": "order.read", "customer_id": "CU-1001"},
        timeout=30,
    )
    assert r.state == TaskState.TASK_STATE_COMPLETED
    data = r.data_parts[0]["data"]
    assert data["order_no"] == "SO-2001"
    assert data["status_name"] == "已签收"


@pytest.mark.asyncio
async def test_clarify_resume(stack):
    """模糊输入 → input-required（澄清）→ 回填 → completed。"""
    port = stack["ports"]["orchestrator"]
    factory, hc = _client(port)
    client = await factory.create_from_url(f"http://127.0.0.1:{port}/")
    try:
        r = await _send_and_collect(client, "帮我处理一下那个东西")
        assert r["state"] == TaskState.TASK_STATE_INPUT_REQUIRED
        assert r["status_data"]["type"] == "clarify"

        from shared.a2a_client import delegate

        r2 = await delegate(
            f"http://127.0.0.1:{port}", "查询 SKU-A001 的商品详情",
            task_id=r["task_id"], context_id=r["context_id"],
        )
        assert r2.state == TaskState.TASK_STATE_COMPLETED
        assert "无线鼠标" in r2.text
    finally:
        await hc.aclose()


@pytest.mark.asyncio
async def test_restart_resume_ac14(stack):
    """AC-14：澄清暂停后重启编排进程，同 taskId 回填仍可恢复完成。"""
    port = stack["ports"]["orchestrator"]
    factory, hc = _client(port)
    client = await factory.create_from_url(f"http://127.0.0.1:{port}/")
    try:
        r = await _send_and_collect(client, "帮我处理一下那个东西")
        assert r["state"] == TaskState.TASK_STATE_INPUT_REQUIRED
    finally:
        await hc.aclose()

    # 重启编排进程（同 SQLITE_DB_PATH → checkpointer/TaskStore 都在）
    stack["procs"]["orchestrator"].terminate()
    stack["procs"]["orchestrator"].wait(timeout=10)
    time.sleep(2)  # Windows：等端口释放
    for _ in range(5):
        stack["procs"]["orchestrator"] = _spawn(
            stack["targets"]["orchestrator"], port, stack["envs"]["orchestrator"]
        )
        if _wait_ready(port, "test-token", timeout=15):
            break
        stack["procs"]["orchestrator"].terminate()
        stack["procs"]["orchestrator"].wait(timeout=5)
        time.sleep(2)
    else:
        pytest.fail("重启后的编排服务未就绪")

    from shared.a2a_client import delegate

    r2 = await delegate(
        f"http://127.0.0.1:{port}", "查询 SKU-A001 的商品详情",
        task_id=r["task_id"], context_id=r["context_id"],
    )
    assert r2.state == TaskState.TASK_STATE_COMPLETED
    assert "无线鼠标" in r2.text
