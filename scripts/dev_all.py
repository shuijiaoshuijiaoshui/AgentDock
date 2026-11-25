"""本机一键拉起 11 个服务（无 Docker 时用；Windows 友好）。

uv run python scripts/dev_all.py
先起 商品域 + 编排 + 5 个桩域，等 Card 端点全部就绪后打印清单；
Ctrl+C 全部退出。
"""

import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from shared.config import get_settings

SERVICES = [
    # (名字, uvicorn 目标, 端口, 额外环境变量)
    ("securityd", "security_service.server:app", None, {}),
    ("merchandising", "domains.merchandising.server:app", None, {}),
    ("customer", "domains.customer.server:app", None, {}),
    ("marketing", "domains.marketing.server:app", None, {}),
    ("orchestrator", "orchestrator.server:app", None, {}),
    ("aftersale", "domains.aftersale.server:app", None, {}),
    ("policy", "domains.policy.server:app", None, {}),
    ("console", "console.server:app", None, {}),
    ("order", "domains.order.server:app", None, {}),
    ("fulfillment", "domains.fulfillment.server:app", None, {}),
    ("finance", "domains.finance.server:app", None, {}),
]


def main() -> None:
    s = get_settings()
    port_key = {
        "merchandising": s.merchandising_port,
        "customer": s.customer_port,
        "marketing": s.marketing_port,
        "orchestrator": s.orchestrator_port,
        "aftersale": s.aftersale_port,
        "policy": s.policy_port,
        "console": s.console_port,
        "order": s.order_port,
        "fulfillment": s.fulfillment_port,
        "finance": s.finance_port,
        "securityd": s.security_port,
    }
    procs: list[tuple[str, subprocess.Popen]] = []
    log_dir = Path(".data/logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    try:
        for name, target, _, extra_env in SERVICES:
            port = port_key[name]
            env = {**os.environ, **extra_env}
            env["SERVICE_NAME"] = name
            # M4（PRD 7.10）：dev 栈默认开 trace，span 落 .data/traces/<服务>.jsonl，
            # 工作台/GET /api/traces/{trace_id} 可查调用树。生产按需显式开启。
            env["TRACING_ENABLED"] = "true"
            if name == "securityd":
                # 单写者进程：独占 security.db，local 模式直写。
                env["SECURITY_MODE"] = "local"
                env["SHARED_DB_PATH"] = s.security_db_path
            else:
                # 业务服务不得直写审计/审批库，统一经 securityd。
                env["SECURITY_MODE"] = "remote"
                env["SECURITY_URL"] = f"http://127.0.0.1:{s.security_port}"
            log_f = open(log_dir / f"{name}.log", "w", encoding="utf-8")  # noqa: SIM115
            p = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", target, "--port", str(port),
                 "--log-level", "warning"],
                env=env, stdout=log_f, stderr=subprocess.STDOUT,
            )
            procs.append((name, p))
            print(f"[dev_all] 启动 {name:<16} :{port}")

        print("[dev_all] 等待 Card 端点就绪……")
        deadline = time.time() + 60
        pending = {name for name, *_ in SERVICES}
        while pending and time.time() < deadline:
            for name in list(pending):
                try:
                    path = "/health" if name == "securityd" else "/.well-known/agent-card.json"
                    r = httpx.get(
                        f"http://127.0.0.1:{port_key[name]}{path}",
                        timeout=2, trust_env=False,
                    )
                    if r.status_code == 200:
                        pending.discard(name)
                except Exception:  # noqa: BLE001
                    pass
            if pending:
                time.sleep(1)
        if pending:
            print(f"[dev_all] 未就绪：{sorted(pending)}（看 .data/logs/<name>.log）")
        else:
            print(f"[dev_all] {len(SERVICES)} 个服务全部就绪 ✔")
            print("[dev_all] 冒烟：uv run python scripts/smoke.py")
        for name, p in procs:
            p.wait()
    except KeyboardInterrupt:
        print("\n[dev_all] 正在退出……")
    finally:
        for name, p in procs:
            p.terminate()
        for name, p in procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        print("[dev_all] 已全部退出")


if __name__ == "__main__":
    main()
