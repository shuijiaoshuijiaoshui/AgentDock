"""M2 e2e：三域并行链（warranty_check）+ 政策/售后单域经编排的真实进程栈。"""

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

os.environ["AUTH_SECRET"] = "test-secret"
os.environ["SHARED_DB_PATH"] = str(Path(tempfile.gettempdir()) / "agentdock-m2-shared.db")

from shared.config import get_settings  # noqa: E402


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
                 ("merchandising", "aftersale", "policy", "orchestrator")}
        # 端口全量下发给每个服务：自己端口用于绑定，其余用于互调回退拼接
        common = {
            **os.environ, "LLM_PROVIDER": "fake", "CHECKPOINTER": "sqlite",
            "DEV_MODE": "true", "AUTH_SECRET": "test-secret",
            "SHARED_DB_PATH": str(Path(tmp) / "shared.db"),
            # 占位：每个服务在 _spawn 前覆盖为自己的库文件（见下方 envs）
            "MERCHANDISING_PORT": str(ports["merchandising"]),
            "AFTERSALE_PORT": str(ports["aftersale"]),
            "POLICY_PORT": str(ports["policy"]),
            "ORCHESTRATOR_PORT": str(ports["orchestrator"]),
        }
        targets = {
            "merchandising": "domains.merchandising.server:app",
            "aftersale": "domains.aftersale.server:app",
            "policy": "domains.policy.server:app",
            "orchestrator": "orchestrator.server:app",
        }
        envs = {n: {**common, "SQLITE_DB_PATH": str(Path(tmp) / f"{n}.sqlite")}
                for n in targets}
        procs = {n: _spawn(t, ports[n], envs[n]) for n, t in targets.items()}
        for n, p in ports.items():
            assert _wait_ready(p, "test-token"), f"{n} :{p} 未就绪"
        yield {"ports": ports, "procs": procs, "targets": targets, "envs": envs}
        for p in procs.values():
            p.terminate()
        for p in procs.values():
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()


@pytest.mark.asyncio
async def test_warranty_check_parallel(stack):
    """三域并行：一次委托 → aftersale/policy/merchandising 三 Artifact 齐备合成。"""
    from a2a.types import TaskState

    from shared.a2a_client import delegate

    r = await delegate(
        f"http://127.0.0.1:{stack['ports']['orchestrator']}",
        "SKU-A001 坏了，保修内还是保修外",
        metadata={"scope": "orchestrator.read", "customer_id": "CU-1001"},
        timeout=60,
    )
    assert r.state == TaskState.TASK_STATE_COMPLETED
    data = r.data_parts[0] if r.data_parts else {}
    arts = data.get("artifacts", {})
    assert set(arts.keys()) == {"aftersale", "policy", "merchandising"}
    assert arts["aftersale"]["data"]["conclusion"] == "保修内"
    assert "CLS-002" in r.text  # 售后 Artifact 引用了生效条款（AC-31）
    assert "无线鼠标" in r.text or "merchandising" in r.text


@pytest.mark.asyncio
async def test_policy_and_aftersale_via_orchestrator(stack):
    """单域链路：政策直答与售后资格经编排路由。"""
    from a2a.types import TaskState

    from shared.a2a_client import delegate

    base = f"http://127.0.0.1:{stack['ports']['orchestrator']}"
    r1 = await delegate(base, "退货政策是什么",
                        metadata={"scope": "orchestrator.read"}, timeout=60)
    assert r1.state == TaskState.TASK_STATE_COMPLETED
    assert "CLS-001" in r1.text

    r2 = await delegate(base, "SKU-A002 还在保修期吗",
                        metadata={"scope": "orchestrator.read",
                                  "customer_id": "CU-1004"}, timeout=60)
    assert r2.state == TaskState.TASK_STATE_COMPLETED
    assert "保修外" in r2.text


@pytest.mark.asyncio
async def test_compound_policy_question_answers_from_knowledge(stack):
    """复合问题不再拿保修/运费条款硬答：权威条款为空 → 知识库兜底并标注非权威。"""
    from a2a.types import TaskState

    from scripts.seed_mock import seed
    from shared.a2a_client import delegate

    seed()
    r = await delegate(
        f"http://127.0.0.1:{stack['ports']['orchestrator']}",
        "最近最新出的关于产品A的购买政策是什么?异地商家应该怎么操作区合作并且购买产品A",
        metadata={"scope": "orchestrator.read"}, timeout=60,
    )
    assert r.state == TaskState.TASK_STATE_COMPLETED
    assert "合作" in r.text and "非权威" in r.text
    assert "整机保修" not in r.text
    assert "已找到生效条款" not in r.text


@pytest.mark.asyncio
async def test_parallel_partial_delivery_when_domain_down(stack):
    """局部交付（PRD 7.3.6 三级）：杀掉政策域，三域并行仍 completed 并标注不可用。"""
    from a2a.types import TaskState

    from shared.a2a_client import delegate

    stack["procs"]["policy"].terminate()
    stack["procs"]["policy"].wait(timeout=10)
    time.sleep(1)  # Windows：等端口释放
    try:
        r = await delegate(
            f"http://127.0.0.1:{stack['ports']['orchestrator']}",
            "SKU-A001 坏了，保修内还是保修外",
            metadata={"scope": "orchestrator.read", "customer_id": "CU-1001"},
        timeout=60,
        )
        assert r.state == TaskState.TASK_STATE_COMPLETED
        data = r.data_parts[0] if r.data_parts else {}
        arts = data.get("artifacts", {})
        # 幸存两域照常给数；政策域缺席但占位标注（局部交付，不整体失败）
        assert arts["aftersale"]["data"]["conclusion"] == "保修内"
        assert arts["policy"]["data"].get("error")
        assert "政策域暂不可用" in r.text  # 局部交付标注（中文域名）
    finally:
        # 拉回政策域，供后续用例（本文件内无后续，稳妥起见）
        stack["procs"]["policy"] = _spawn(
            stack["targets"]["policy"], stack["ports"]["policy"], stack["envs"]["policy"])
        assert _wait_ready(stack["ports"]["policy"], "test-token", timeout=20)
