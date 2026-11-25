"""评测全栈：拉起 10 个真服务进程（临时目录/自由端口）+ 两类执行表面。

栈拉起模式照 tests/test_customer_e2e.py / scripts/dev_all.py：
- tempfile.TemporaryDirectory(ignore_cleanup_errors=True)（Windows：sqlite 句柄滞留）
- 自由端口 + 就绪探针（securityd /health，其余 agent-card.json）
- teardown terminate → wait(5) → kill
- 全部 loopback httpx trust_env=False（E12：Windows 系统代理劫持）

表面：
- console：客户真实 HTTP 路径（登录/CSRF/send/messages/actions/审批面板）
- a2a：shared.a2a_client.delegate + service JWT（产物/decision_log 可见）
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from a2a.types import TaskState  # noqa: E402

from shared.a2a_client import delegate  # noqa: E402
from shared.auth import ALL_SCOPES, create_token, decode_token  # noqa: E402
from shared.tracing import new_trace_id  # noqa: E402

DEMO_PASSWORD = "demo123"

# (名字, uvicorn 目标)——与 scripts/dev_all.py SERVICES 一致
TARGETS = {
    "securityd": "security_service.server:app",
    "merchandising": "domains.merchandising.server:app",
    "customer": "domains.customer.server:app",
    "marketing": "domains.marketing.server:app",
    "orchestrator": "orchestrator.server:app",
    "aftersale": "domains.aftersale.server:app",
    "policy": "domains.policy.server:app",
    "console": "console.server:app",
    "order": "domains.order.server:app",
    "fulfillment": "domains.fulfillment.server:app",
    "finance": "domains.finance.server:app",
}


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _spawn(target: str, port: int, env: dict, log_dir: Path) -> subprocess.Popen:
    log_dir.mkdir(parents=True, exist_ok=True)
    name = target.split(":")[0].replace(".", "_")
    log = open(log_dir / f"eval_{name}.log", "w", encoding="utf-8")  # noqa: SIM115
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


def _stop(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


class EvalStack:
    """一套评测专用栈：临时 DB + 真 10 进程 + 追踪/审计落盘。"""

    def __init__(self, provider: str = "fake",
                 retriever: str = "keyword") -> None:
        self.provider = provider
        self.retriever = retriever
        # 注意：不在这里改 os.environ——评测栈的进程级影响必须进出栈配对
        #（start 保存 / stop 还原），否则会污染同进程里的其他测试。
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        tmp = Path(self._tmp.name)
        self.tmp = tmp
        self.business_db = str(tmp / "mock.db")
        self.security_db = str(tmp / "security.db")
        self.console_db = str(tmp / "console.db")
        self.trace_dir = str(tmp / "traces")
        # 日志放持久目录：栈排障需要（tmp 会随 cleanup 消失）
        self.log_dir = ROOT / ".data" / "logs" / "eval"
        self.ports = {n: _free_port() for n in TARGETS}
        self.procs: dict[str, subprocess.Popen] = {}
        self._saved_env: dict[str, str | None] = {}

    # -- 栈生命周期 ---------------------------------------------------------

    _ENV_KEYS = ("AUTH_SECRET", "ENV", "BUSINESS_DB_PATH", "SECURITY_MODE",
                 "SHARED_DB_PATH")

    def _push_env(self) -> None:
        self._saved_env = {k: os.environ.get(k) for k in self._ENV_KEYS}

    def _pop_env(self) -> None:
        for k, v in getattr(self, "_saved_env", {}).items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        from shared.config import get_settings

        get_settings.cache_clear()

    def _seed_subprocess(self, business_db: str | None = None) -> None:
        """子进程跑 seed_mock：它的 DB 路径在导入时固化，绝不能在本进程导入。"""
        env = {**os.environ}
        if business_db:
            env["BUSINESS_DB_PATH"] = business_db
        else:
            env.pop("BUSINESS_DB_PATH", None)
        subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "seed_mock.py")],
            env=env, cwd=ROOT, check=True, capture_output=True,
        )

    def start(self) -> "EvalStack":
        self._push_env()
        # eval 进程自身也要读审计/落追踪：绑到本套栈的 security.db（local 直读）。
        # 注意：seed_mock 的 DB 路径在模块导入时解析——本项目内只许子进程 seed。
        os.environ["BUSINESS_DB_PATH"] = self.business_db
        os.environ["SECURITY_MODE"] = "local"
        os.environ["SHARED_DB_PATH"] = self.security_db
        os.environ["AUTH_SECRET"] = "test-secret"
        from shared.config import get_settings

        get_settings.cache_clear()
        from . import evidence as ev

        ev.bind_audit_db(self.security_db)
        self._seed_subprocess(self.business_db)
        sec_env = {
            **os.environ, "ENV": "test", "AUTH_SECRET": "test-secret",
            "SECURITY_MODE": "local", "SECURITY_DB_PATH": self.security_db,
            "SHARED_DB_PATH": self.security_db,
            "SERVICE_NAME": "securityd", "TRACING_ENABLED": "true",
            "TRACE_DIR": self.trace_dir, "BUSINESS_DB_PATH": self.business_db,
        }
        self.procs["securityd"] = _spawn(
            TARGETS["securityd"], self.ports["securityd"], sec_env, self.log_dir)
        assert _wait(f"http://127.0.0.1:{self.ports['securityd']}/health"), \
            "securityd 未就绪（见 evals 日志）"

        common = {
            **os.environ, "ENV": "test", "AUTH_SECRET": "test-secret",
            "LLM_PROVIDER": self.provider, "CHECKPOINTER": "sqlite",
            "DEV_MODE": "true", "RATE_LIMIT_ENABLED": "false",
            "POLICY_RETRIEVER": self.retriever,
            "SECURITY_MODE": "remote",
            "SECURITY_URL": f"http://127.0.0.1:{self.ports['securityd']}",
            "SHARED_DB_PATH": str(self.tmp / "shared.db"),
            "CONSOLE_DB_PATH": self.console_db,
            "BUSINESS_DB_PATH": self.business_db,
            "TRACING_ENABLED": "true", "TRACE_DIR": self.trace_dir,
        }
        port_keys = {
            "orchestrator": "ORCHESTRATOR_PORT",
            "merchandising": "MERCHANDISING_PORT",
            "customer": "CUSTOMER_PORT",
            "marketing": "MARKETING_PORT",
            "order": "ORDER_PORT",
            "fulfillment": "FULFILLMENT_PORT",
            "aftersale": "AFTERSALE_PORT",
            "finance": "FINANCE_PORT",
            "policy": "POLICY_PORT",
            "console": "CONSOLE_PORT",
        }
        # 每个服务都拿到全部端口覆盖：console 要连 orchestrator，
        # orchestrator 要连各域（缺了就回落 dev 默认端口 → 串台）
        all_ports = {k: str(self.ports[n]) for n, k in port_keys.items()}
        for name, target in TARGETS.items():
            if name == "securityd":
                continue
            env = {
                **common, **all_ports,
                "SQLITE_DB_PATH": str(self.tmp / f"{name}.sqlite"),
                "SERVICE_NAME": name, port_keys[name]: str(self.ports[name]),
            }
            self.procs[name] = _spawn(target, self.ports[name], env, self.log_dir)
        for name, port in self.ports.items():
            if name == "securityd":
                continue
            assert _wait(f"http://127.0.0.1:{port}/.well-known/agent-card.json"), \
                f"{name} 未就绪（见 evals 日志）"
        return self

    def stop(self) -> None:
        try:
            for proc in self.procs.values():
                _stop(proc)
            self.procs.clear()
        finally:
            # 还原进程环境并重绑审计库——评测绝不能污染宿主进程
            self._pop_env()
            from shared.security_db import reset_for_tests

            reset_for_tests()
            self._seed_subprocess()
            self._tmp.cleanup()

    def __enter__(self) -> "EvalStack":
        return self.start()

    def __exit__(self, *exc) -> None:  # noqa: ANN002
        self.stop()

    # -- 进程级故障注入（E-R2 用真实杀进程） ---------------------------------

    def kill_service(self, name: str) -> None:
        proc = self.procs.pop(name, None)
        if proc is not None:
            _stop(proc)
        time.sleep(1.5)  # Windows：等端口释放

    def start_service(self, name: str) -> None:
        if name in self.procs:
            return
        sec_url = f"http://127.0.0.1:{self.ports['securityd']}"
        env = {
            **os.environ, "ENV": "test", "AUTH_SECRET": "test-secret",
            "LLM_PROVIDER": self.provider, "CHECKPOINTER": "sqlite",
            "DEV_MODE": "true", "RATE_LIMIT_ENABLED": "false",
            "POLICY_RETRIEVER": self.retriever,
            "SECURITY_MODE": "remote", "SECURITY_URL": sec_url,
            "SHARED_DB_PATH": str(self.tmp / "shared.db"),
            "CONSOLE_DB_PATH": self.console_db,
            "BUSINESS_DB_PATH": self.business_db,
            "TRACING_ENABLED": "true", "TRACE_DIR": self.trace_dir,
            "SQLITE_DB_PATH": str(self.tmp / f"{name}.sqlite"),
            "SERVICE_NAME": name,
        }
        port_key = f"{name.upper()}_PORT"
        env[port_key] = str(self.ports[name])
        for other, key in (("orchestrator", "ORCHESTRATOR_PORT"),
                           ("console", "CONSOLE_PORT"),
                           ("merchandising", "MERCHANDISING_PORT"),
                           ("customer", "CUSTOMER_PORT"),
                           ("marketing", "MARKETING_PORT"),
                           ("order", "ORDER_PORT"),
                           ("fulfillment", "FULFILLMENT_PORT"),
                           ("aftersale", "AFTERSALE_PORT"),
                           ("finance", "FINANCE_PORT"),
                           ("policy", "POLICY_PORT")):
            env[key] = str(self.ports[other])
        self.procs[name] = _spawn(
            TARGETS[name], self.ports[name], env, self.log_dir)
        assert _wait(f"http://127.0.0.1:{self.ports[name]}"
                     f"/.well-known/agent-card.json"), f"{name} 重启未就绪"

    # -- URL ----------------------------------------------------------------

    def url(self, name: str) -> str:
        return f"http://127.0.0.1:{self.ports[name]}"

    @property
    def orchestrator_url(self) -> str:
        return self.url("orchestrator")

    @property
    def console_url(self) -> str:
        return self.url("console")

    # -- 业务库重置（每 trial 全量回种子态） ----------------------------------

    def reseed(self) -> None:
        self._seed_subprocess(self.business_db)

    # -- A2A 表面 ------------------------------------------------------------

    def service_auth(self, tenant: str = "t_demo"):
        token = create_token(
            tenant_id=tenant, actor_id="agentdock-eval", role="service",
            scopes=ALL_SCOPES, token_type="service",
        )
        return decode_token(token)

    async def a2a(self, text: str, *, customer_id: str = "CU-1001",
                  tenant: str = "t_demo", metadata: dict | None = None,
                  task_id: str | None = None, context_id: str | None = None,
                  resume_text: str | None = None, timeout: float = 90):
        meta = {"customer_id": customer_id, "trace_id": new_trace_id()}
        meta.update(metadata or {})
        r = await delegate(
            self.orchestrator_url, text, task_id=task_id,
            context_id=context_id, metadata=meta, timeout=timeout,
            auth=self.service_auth(tenant),
        )
        if resume_text and r.state == TaskState.TASK_STATE_INPUT_REQUIRED:
            r = await delegate(
                self.orchestrator_url, resume_text, task_id=r.task_id,
                context_id=r.context_id, metadata=meta, timeout=timeout,
                auth=self.service_auth(tenant),
            )
        return r


class ConsoleClient:
    """单角色会话：客户（customer 表面）或坐席（workbench 表面）。"""

    def __init__(self, base_url: str) -> None:
        self.base = base_url.rstrip("/")
        self.http = httpx.Client(
            base_url=self.base, trust_env=False, timeout=120)
        self.token = ""
        self.csrf = ""

    def login(self, tenant: str, username: str,
              surface: str = "customer") -> None:
        r = self.http.post("/api/auth/login", json={
            "tenant_id": tenant, "username": username,
            "password": DEMO_PASSWORD, "surface": surface,
        })
        assert r.status_code == 200, f"登录失败 {tenant}/{username}: {r.text}"
        data = r.json()
        self.token = data["token"]
        self.csrf = data.get("csrf_token", "")

    def _headers(self, write: bool = False) -> dict:
        h = {"Authorization": f"Bearer {self.token}"}
        if write and self.csrf:
            h["X-CSRF-Token"] = self.csrf
        return h

    # -- 客户表面 --------------------------------------------------------

    def conversation(self) -> dict:
        r = self.http.get("/api/customer/conversation", headers=self._headers())
        assert r.status_code == 200, r.text
        return r.json()

    def send(self, text: str, cid: str | None = None) -> dict:
        body: dict = {"text": text}
        if cid:
            body["conversation_id"] = cid
        r = self.http.post("/api/customer/send", headers=self._headers(True),
                           json=body)
        assert r.status_code == 200, f"send 失败: {r.text}"
        return r.json()

    def messages(self, cid: str, after_id: int = 0) -> list:
        r = self.http.get(f"/api/conversations/{cid}/messages",
                          params={"after_id": after_id},
                          headers=self._headers())
        assert r.status_code == 200, r.text
        return r.json()

    def confirm(self, cid: str, action_id: str) -> dict:
        r = self.http.post(f"/api/conversations/{cid}/actions",
                           headers=self._headers(True),
                           json={"action_id": action_id})
        return r

    def card_action(self, cid: str, action_id: str, params: dict) -> dict:
        r = self.http.post(f"/api/conversations/{cid}/card-action",
                           headers=self._headers(True),
                           json={"action_id": action_id, "params": params})
        return r

    def close_conversation(self, cid: str) -> None:
        self.http.post(f"/api/conversations/{cid}/close",
                       headers=self._headers(True))

    # -- 坐席表面（审批面板） ----------------------------------------------

    def approvals(self, status: str | None = None) -> list:
        params = {"status": status} if status else None
        r = self.http.get("/api/approvals", params=params,
                          headers=self._headers())
        assert r.status_code == 200, r.text
        return r.json()

    def approve(self, approval_id: str) -> httpx.Response:
        return self.http.post(f"/api/approvals/{approval_id}/approve",
                              headers=self._headers(True))

    def reject(self, approval_id: str) -> httpx.Response:
        return self.http.post(f"/api/approvals/{approval_id}/reject",
                              headers=self._headers(True))

    def execute(self, approval_id: str) -> httpx.Response:
        return self.http.post(f"/api/approvals/{approval_id}/execute",
                              headers=self._headers(True))

    def close(self) -> None:
        self.http.close()


def state_name_of(result) -> str:  # noqa: ANN001
    """protobuf 枚举名（如 TASK_STATE_COMPLETED）；str(enum) 是数字，不能用。"""
    if not result:
        return ""
    try:
        return TaskState.Name(result.state)
    except Exception:  # noqa: BLE001
        return str(result.state)


def is_completed(result) -> bool:  # noqa: ANN001
    return bool(result) and result.state == TaskState.TASK_STATE_COMPLETED


async def a2a_task(stack: EvalStack, text: str, **kw) -> dict:  # noqa: ANN003
    """a2a 委托并收集成 checker 可用的 dict（含澄清自动回填）。"""
    resume_text = kw.pop("resume_text", None)
    r = await stack.a2a(text, **kw)
    clarify = None
    if resume_text and r.state == TaskState.TASK_STATE_INPUT_REQUIRED:
        clarify = {"state": state_name_of(r), "status_data": r.status_data}
        # 回填复用同一 metadata（含同一 trace_id）——两跳同 trace 可追溯
        r = await stack.a2a(resume_text, task_id=r.task_id,
                            context_id=r.context_id, **kw)
    final = r.data_parts[0] if r.data_parts else {}
    return {
        "state": state_name_of(r),
        "completed": is_completed(r),
        "text": r.text or "",
        "final": final,
        "artifacts": final.get("artifacts") or {},
        "decision_log": final.get("decision_log") or [],
        "status_data": r.status_data,
        "task_id": r.task_id,
        "context_id": r.context_id,
        "clarify_result": clarify,
    }


def run_async(coro):  # noqa: ANN001
    return asyncio.run(coro)
