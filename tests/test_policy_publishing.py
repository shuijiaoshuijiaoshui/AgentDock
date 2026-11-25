"""政策发布工作流 e2e：四眼审批、权限、版本化发布、审计、租户隔离。

栈：securityd + console（业务库为临时目录，测试前后子进程 reseed）。
"""

import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _spawn(target: str, port: int, env: dict):  # noqa: ANN001, ANN201
    log_dir = ROOT / ".data" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log = open(log_dir / f"policy_pub_{target.split(':')[0]}.log", "w",
              encoding="utf-8")  # noqa: SIM115
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


def _seed(business_db: str) -> None:
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "seed_mock.py")],
        env={**os.environ, "BUSINESS_DB_PATH": business_db},
        cwd=ROOT, check=True, capture_output=True,
    )


@pytest.fixture(scope="module")
def stack():
    from shared.config import get_settings

    get_settings.cache_clear()
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        tmp_path = Path(tmp)
        business_db = str(tmp_path / "mock.db")
        security_db = str(tmp_path / "security.db")
        _seed(business_db)
        ports = {n: _free_port() for n in ("securityd", "console")}
        sec_env = {
            **os.environ, "ENV": "test", "AUTH_SECRET": "test-secret",
            "SECURITY_MODE": "local", "SECURITY_DB_PATH": security_db,
            "SHARED_DB_PATH": security_db, "SERVICE_NAME": "securityd",
            "BUSINESS_DB_PATH": business_db,
        }
        sec = _spawn("security_service.server:app", ports["securityd"], sec_env)
        try:
            assert _wait(f"http://127.0.0.1:{ports['securityd']}/health"), \
                "securityd 未就绪"
            console_env = {
                **os.environ, "ENV": "test", "LLM_PROVIDER": "fake",
                "CHECKPOINTER": "sqlite", "AUTH_SECRET": "test-secret",
                "RATE_LIMIT_ENABLED": "false", "SECURITY_MODE": "remote",
                "SECURITY_URL": f"http://127.0.0.1:{ports['securityd']}",
                "SHARED_DB_PATH": str(tmp_path / "shared.db"),
                "CONSOLE_DB_PATH": str(tmp_path / "console.db"),
                "BUSINESS_DB_PATH": business_db,
                "SERVICE_NAME": "console",
                "TRACING_ENABLED": "true",
                "TRACE_DIR": str(tmp_path / "traces"),
            }
            console = _spawn("console.server:app", ports["console"], console_env)
            try:
                assert _wait(
                    f"http://127.0.0.1:{ports['console']}/.well-known/agent-card.json"
                ), "console 未就绪"
                yield {"base": f"http://127.0.0.1:{ports['console']}",
                       "business_db": business_db, "security_db": security_db}
            finally:
                console.terminate()
                try:
                    console.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    console.kill()
        finally:
            sec.terminate()
            try:
                sec.wait(timeout=5)
            except subprocess.TimeoutExpired:
                sec.kill()


@pytest.fixture(autouse=True)
def _fresh_business_db(stack):  # noqa: ANN001
    _seed(stack["business_db"])
    yield
    _seed(stack["business_db"])


def _login(stack, tenant: str, username: str) -> dict:  # noqa: ANN001
    r = httpx.post(
        stack["base"] + "/api/auth/login",
        json={"tenant_id": tenant, "username": username,
              "password": "demo123", "surface": "workbench"},
        timeout=10, trust_env=False,
    )
    assert r.status_code == 200, r.text
    data = r.json()
    return {"Authorization": f"Bearer {data['token']}",
            "X-CSRF-Token": data.get("csrf_token", "")}


def _draft_payload(**over) -> dict:  # noqa: ANN003
    today = date.today().isoformat()
    body = {
        "clause_id": "CLS-002",
        "title": "整机保修（2026 修订）",
        "text": "自购买日起 24 个月内整机保修，人为损坏不在保修范围。",
        "keywords": "保修 维修 质保 两年 24个月",
        "effective_from": today,
    }
    body.update(over)
    return body


def _db_one(stack, sql: str, args: tuple = ()):  # noqa: ANN001
    conn = sqlite3.connect(stack["business_db"])
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, args).fetchone()
    finally:
        conn.close()


def _create_submit(stack, headers) -> str:  # noqa: ANN001
    created = httpx.post(stack["base"] + "/api/policy/drafts",
                         headers=headers, json=_draft_payload(),
                         timeout=10, trust_env=False)
    assert created.status_code == 201, created.text
    draft_id = created.json()["draft"]["draft_id"]
    sub = httpx.post(
        stack["base"] + f"/api/policy/drafts/{draft_id}/submit",
        headers=headers, json={}, timeout=10, trust_env=False)
    assert sub.status_code == 200, sub.text
    return draft_id


def test_publish_flow_creates_version_and_closes_old(stack):
    supervisor = _login(stack, "t_demo", "supervisor")
    admin = _login(stack, "t_demo", "admin")
    draft_id = _create_submit(stack, supervisor)

    approved = httpx.post(
        stack["base"] + f"/api/policy/drafts/{draft_id}/approve",
        headers=admin, json={}, timeout=10, trust_env=False)
    assert approved.status_code == 200, approved.text

    detail = httpx.get(
        stack["base"] + f"/api/policy/drafts/{draft_id}",
        headers=admin, timeout=10, trust_env=False)
    assert detail.status_code == 200
    assert "text" in detail.json()["diff"]["changed"]

    published = httpx.post(
        stack["base"] + f"/api/policy/drafts/{draft_id}/publish",
        headers=admin, json={}, timeout=10, trust_env=False)
    assert published.status_code == 200, published.text
    draft = published.json()["draft"]
    assert draft["status"] == "published"
    assert draft["version"] == "v2"

    new_row = _db_one(stack, "SELECT * FROM policy_docs WHERE tenant_id='t_demo' "
                             "AND clause_id='CLS-002' AND version='v2'")
    assert new_row is not None
    assert new_row["effective_from"] == date.today().isoformat()
    old = _db_one(stack, "SELECT * FROM policy_docs WHERE tenant_id='t_demo' "
                         "AND clause_id='CLS-002' AND version='v1'")
    assert old["effective_to"] == (
        date.today() - timedelta(days=1)).isoformat()


def test_four_eyes_blocks_author_approval(stack):
    supervisor = _login(stack, "t_demo", "supervisor")
    draft_id = _create_submit(stack, supervisor)
    same = httpx.post(
        stack["base"] + f"/api/policy/drafts/{draft_id}/approve",
        headers=supervisor, json={}, timeout=10, trust_env=False)
    assert same.status_code == 403
    assert same.json()["error"] == "four_eyes"


def test_agent_cannot_create_policy_draft(stack):
    agent = _login(stack, "t_demo", "agent")
    r = httpx.post(stack["base"] + "/api/policy/drafts", headers=agent,
                   json=_draft_payload(), timeout=10, trust_env=False)
    assert r.status_code == 403
    assert r.json()["error"] == "supervisor_required"


def test_backdated_publish_rejected(stack):
    supervisor = _login(stack, "t_demo", "supervisor")
    admin = _login(stack, "t_demo", "admin")
    past = (date.today() - timedelta(days=30)).isoformat()
    created = httpx.post(stack["base"] + "/api/policy/drafts",
                         headers=supervisor,
                         json=_draft_payload(effective_from=past),
                         timeout=10, trust_env=False)
    draft_id = created.json()["draft"]["draft_id"]
    httpx.post(stack["base"] + f"/api/policy/drafts/{draft_id}/submit",
               headers=supervisor, json={}, timeout=10, trust_env=False)
    httpx.post(stack["base"] + f"/api/policy/drafts/{draft_id}/approve",
               headers=admin, json={}, timeout=10, trust_env=False)
    r = httpx.post(stack["base"] + f"/api/policy/drafts/{draft_id}/publish",
                   headers=admin, json={}, timeout=10, trust_env=False)
    assert r.status_code == 409
    assert r.json()["error"] == "backdated"


def test_tenant_isolation_and_audit(stack):
    supervisor = _login(stack, "t_demo", "supervisor")
    draft_id = _create_submit(stack, supervisor)

    other = _login(stack, "t_other", "supervisor2")
    peek = httpx.get(stack["base"] + f"/api/policy/drafts/{draft_id}",
                     headers=other, timeout=10, trust_env=False)
    assert peek.status_code == 404

    events = httpx.get(stack["base"] + "/api/audit/events?limit=300",
                       headers=supervisor, timeout=15, trust_env=False).json()
    actions = {e["action"] for e in events}
    assert "console.policy_draft_created" in actions
    assert "console.policy_draft_submitted" in actions


def test_keywords_auto_generated_when_omitted(stack):
    supervisor = _login(stack, "t_demo", "supervisor")
    payload = _draft_payload()
    payload.pop("keywords")
    created = httpx.post(stack["base"] + "/api/policy/drafts",
                         headers=supervisor, json=payload,
                         timeout=10, trust_env=False)
    assert created.status_code == 201, created.text
    assert created.json()["draft"]["keywords"]


def test_document_import_creates_drafts_and_outbox(stack):
    supervisor = _login(stack, "t_demo", "supervisor")
    admin = _login(stack, "t_demo", "admin")
    markdown = (
        "## CLS-006 安装服务\n"
        "安装由认证工程师上门完成，偏远地区需提前预约。\n\n"
        "## CLS-007 延保服务\n"
        "延保需在购买后 30 日内申请，覆盖原厂保修外 12 个月。\n"
    )
    r = httpx.post(
        stack["base"] + "/api/policy/documents", headers=supervisor,
        files={"file": ("policy.md", markdown.encode("utf-8"),
                        "text/markdown")},
        timeout=20, trust_env=False,
    )
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["clause_count"] == 2
    assert [d["clause_id"] for d in data["drafts"]] == ["CLS-006", "CLS-007"]
    assert all(d["source_doc_id"] == data["doc_id"] for d in data["drafts"])

    doc = _db_one(stack, "SELECT * FROM policy_documents WHERE tenant_id='t_demo' "
                         "AND doc_id=?", (data["doc_id"],))
    assert doc is not None and doc["clause_count"] == 2

    draft_id = data["drafts"][0]["draft_id"]
    httpx.post(stack["base"] + f"/api/policy/drafts/{draft_id}/submit",
               headers=supervisor, json={}, timeout=10, trust_env=False)
    httpx.post(stack["base"] + f"/api/policy/drafts/{draft_id}/approve",
               headers=admin, json={}, timeout=10, trust_env=False)
    published = httpx.post(
        stack["base"] + f"/api/policy/drafts/{draft_id}/publish",
        headers=admin, json={}, timeout=10, trust_env=False)
    assert published.status_code == 200, published.text
    outbox = _db_one(
        stack, "SELECT * FROM policy_index_outbox WHERE tenant_id='t_demo' "
               "AND clause_id='CLS-006'")
    assert outbox is not None
    assert outbox["status"] == "pending"


def test_document_import_rejects_bad_format(stack):
    supervisor = _login(stack, "t_demo", "supervisor")
    r = httpx.post(
        stack["base"] + "/api/policy/documents", headers=supervisor,
        files={"file": ("policy.md", b"no headings here", "text/markdown")},
        timeout=10, trust_env=False,
    )
    assert r.status_code == 400
    assert r.json()["error"] == "no_clause_heading"
