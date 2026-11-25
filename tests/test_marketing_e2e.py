"""营销经营分析域端到端（真进程）：单域投放分析、爆款→履约仲裁联动、
杀进程局部交付。复用 m2/customer e2e 的 stack 模式（fake LLM，无外部依赖）。"""

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

from scripts.seed_mock import seed  # noqa: E402
from shared.config import get_settings  # noqa: E402

os.environ.setdefault("AUTH_SECRET", "test-secret")


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
    log = open(log_dir / f"mk_{name}.log", "w", encoding="utf-8")  # noqa: SIM115
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
            ("securityd", "orchestrator", "marketing", "fulfillment")
        }
        security_db = str(Path(tmp) / "security.db")
        sec = _spawn("security_service.server:app", ports["securityd"], {
            **os.environ, "ENV": "test", "AUTH_SECRET": "test-secret",
            "SECURITY_MODE": "local", "SECURITY_DB_PATH": security_db,
            "SHARED_DB_PATH": security_db, "SERVICE_NAME": "securityd",
        })
        procs: dict[str, subprocess.Popen] = {}
        try:
            assert _wait(f"http://127.0.0.1:{ports['securityd']}/health"), \
                "securityd 未就绪"
            common = {
                **os.environ, "ENV": "test", "LLM_PROVIDER": "fake",
                "CHECKPOINTER": "sqlite", "AUTH_SECRET": "test-secret",
                "RATE_LIMIT_ENABLED": "false", "DEV_MODE": "true",
                "SECURITY_MODE": "remote",
                "SECURITY_URL": f"http://127.0.0.1:{ports['securityd']}",
                "SHARED_DB_PATH": str(Path(tmp) / "shared.db"),
                "ORCHESTRATOR_PORT": str(ports["orchestrator"]),
                "MARKETING_PORT": str(ports["marketing"]),
                "FULFILLMENT_PORT": str(ports["fulfillment"]),
            }
            targets = {
                "orchestrator": "orchestrator.server:app",
                "marketing": "domains.marketing.server:app",
                "fulfillment": "domains.fulfillment.server:app",
            }
            for name, target in targets.items():
                procs[name] = _spawn(target, ports[name], {
                    **common,
                    "SQLITE_DB_PATH": str(Path(tmp) / f"{name}.sqlite"),
                    "SERVICE_NAME": name,
                })
            for name, port in ports.items():
                if name == "securityd":
                    continue
                assert _wait(
                    f"http://127.0.0.1:{port}/.well-known/agent-card.json"), \
                    f"{name} 未就绪"
            yield {"ports": ports, "procs": procs}
        finally:
            for proc in procs.values():
                proc.terminate()
            for proc in procs.values():
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
            sec.terminate()
            try:
                sec.wait(timeout=5)
            except subprocess.TimeoutExpired:
                sec.kill()
            seed()


@pytest.mark.asyncio
async def test_ad_analysis_single_domain(stack):
    """投流效果单域链：营销域产物 + 确定性方案（加投/暂停）。"""
    from a2a.types import TaskState

    from shared.a2a_client import delegate

    r = await delegate(
        f"http://127.0.0.1:{stack['ports']['orchestrator']}",
        "看下最近14天的投流效果",
        metadata={"scope": "orchestrator.read", "customer_id": "CU-1001"},
        timeout=60,
    )
    assert r.state == TaskState.TASK_STATE_COMPLETED
    data = r.data_parts[0] if r.data_parts else {}
    arts = data.get("artifacts", {})
    assert set(arts.keys()) == {"marketing"}
    assert arts["marketing"]["authority_domain"] == "marketing"
    assert arts["marketing"]["data"]["total"]["roi"] > 0
    assert "ROI" in r.text and ("加投" in r.text or "暂停" in r.text)


@pytest.mark.asyncio
async def test_hot_restock_arbitrate_injects_top_sku(stack):
    """爆款→备货仲裁链：marketing(top_sku) 注入 fulfillment 查同一 SKU。"""
    from a2a.types import TaskState

    from shared.a2a_client import delegate

    r = await delegate(
        f"http://127.0.0.1:{stack['ports']['orchestrator']}",
        "啥卖得最火，需要补货吗",
        metadata={"scope": "orchestrator.read", "customer_id": "CU-1001"},
        timeout=60,
    )
    assert r.state == TaskState.TASK_STATE_COMPLETED
    data = r.data_parts[0] if r.data_parts else {}
    arts = data.get("artifacts", {})
    assert arts["marketing"]["data"]["top_sku"] == "SKU-A003"
    assert arts["fulfillment"]["data"]["sku"] == "SKU-A003"
    assert "SKU-A003" in r.text


@pytest.mark.asyncio
async def test_marketing_down_partial_delivery(stack):
    """杀掉营销域进程：其余意图不受影响；营销链局部交付并明确标注。"""
    from a2a.types import TaskState

    from shared.a2a_client import delegate

    proc = stack["procs"]["marketing"]
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()

    base = f"http://127.0.0.1:{stack['ports']['orchestrator']}"
    # 营销链：缺席标注，不整体报错
    r = await delegate(
        base, "看下最近14天的投流效果",
        metadata={"scope": "orchestrator.read", "customer_id": "CU-1001"},
        timeout=60,
    )
    assert r.state == TaskState.TASK_STATE_COMPLETED
    assert "营销分析域暂不可用" in r.text
