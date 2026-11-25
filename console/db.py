"""人工坐席工作台存储（console.db）：租户/身份、会话、消息、转人工队列、审计。

M1 单机 sqlite；生产换 Postgres（与 a2a TaskStore 同一套基础设施）。
所有业务查询强制带 tenant_id；tenant 默认值仅用于旧测试/旧数据迁移，入口层必须
从 JWT claim 传入。
"""

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from shared.auth import ROLE_SCOPES, hash_password

DEFAULT_TENANT = "t_demo"

# 可配置（e2e 隔离用）；默认落项目根 .data/
DB_PATH = os.environ.get("CONSOLE_DB_PATH", ".data/console.db")

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def _get() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        with _conn:
            _conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tenants (
                    tenant_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    plan TEXT NOT NULL DEFAULT 'basic',
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS users (
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    username TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL,
                    scopes TEXT NOT NULL,
                    display_name TEXT,
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, user_id),
                    UNIQUE (tenant_id, username)
                );
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,           -- 即 A2A contextId
                    tenant_id TEXT NOT NULL DEFAULT 't_demo',
                    mode TEXT NOT NULL DEFAULT 'ai',      -- ai | human
                    status TEXT NOT NULL DEFAULT 'active',-- active | pending_handoff | with_human | review | closed
                    state TEXT NOT NULL DEFAULT 'AI_ACTIVE', -- 2A 状态机
                    wall_count INTEGER NOT NULL DEFAULT 0,
                    pending_task_id TEXT,          -- 澄清 interrupt 挂起的编排 Task（回填用）
                    customer_id TEXT,              -- 客户身份（来自登录 token，PRD 8.2 行级依据）
                    assigned_agent_id TEXT,        -- 接管坐席
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id TEXT NOT NULL DEFAULT 't_demo',
                    conversation_id TEXT NOT NULL,
                    role TEXT NOT NULL,            -- customer | ai | agent | system
                    content TEXT NOT NULL,
                    meta TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS handoff_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id TEXT NOT NULL DEFAULT 't_demo',
                    conversation_id TEXT NOT NULL,
                    trace_id TEXT,
                    reason TEXT,
                    package TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending', -- pending | taken | returned
                    taken_by TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS pending_actions (
                    action_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL DEFAULT 't_demo',
                    conversation_id TEXT NOT NULL,
                    trace_id TEXT,
                    type TEXT NOT NULL,            -- confirm | approval
                    domain TEXT,
                    skill TEXT,
                    payload TEXT NOT NULL,         -- JSON {intent, slots}
                    preview TEXT,
                    confirm_token TEXT,
                    approval_id TEXT,
                    status TEXT NOT NULL DEFAULT 'pending', -- pending | executed | cancelled | expired
                    expires_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_pending_actions_conv
                    ON pending_actions(tenant_id, conversation_id, status);
                CREATE TABLE IF NOT EXISTS agent_runs (
                    run_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL DEFAULT 't_demo',
                    conversation_id TEXT NOT NULL,
                    trace_id TEXT,
                    intent TEXT,
                    model TEXT,
                    prompt_version TEXT,
                    status TEXT NOT NULL DEFAULT 'completed',
                    error TEXT,
                    started_at TEXT,
                    finished_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_agent_runs_conv
                    ON agent_runs(tenant_id, conversation_id, finished_at);
                CREATE TABLE IF NOT EXISTS agent_steps (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL DEFAULT 't_demo',
                    conversation_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    stage TEXT NOT NULL,
                    domain TEXT,
                    tool TEXT,
                    input_summary TEXT,
                    output_summary TEXT,
                    latency_ms INTEGER,
                    status TEXT,
                    error TEXT,
                    prompt_version TEXT,
                    model TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_agent_steps_run
                    ON agent_steps(tenant_id, run_id, seq);
                CREATE TABLE IF NOT EXISTS attachments (
                    attachment_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL DEFAULT 't_demo',
                    conversation_id TEXT,
                    message_id INTEGER,
                    uploader_id TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    mime TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    storage_path TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_attachments_conv
                    ON attachments(tenant_id, conversation_id);
                CREATE TABLE IF NOT EXISTS feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id TEXT NOT NULL DEFAULT 't_demo',
                    conversation_id TEXT NOT NULL,
                    message_id INTEGER NOT NULL,
                    agent_answer TEXT,
                    edited_answer TEXT,
                    reason TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_feedback_conv
                    ON feedback(tenant_id, conversation_id);
                CREATE TABLE IF NOT EXISTS assist_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id TEXT NOT NULL DEFAULT 't_demo',
                    agent_id TEXT,
                    role TEXT NOT NULL,            -- agent（坐席问） | ai（主 Agent 答）
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS conversation_notes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id TEXT NOT NULL DEFAULT 't_demo',
                    conversation_id TEXT NOT NULL,
                    author_id TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_notes_conv
                    ON conversation_notes(tenant_id, conversation_id, id);
                CREATE TABLE IF NOT EXISTS conversation_reads (
                    tenant_id TEXT NOT NULL DEFAULT 't_demo',
                    conversation_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    last_read_message_id INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, conversation_id, user_id)
                );
                CREATE TABLE IF NOT EXISTS conversation_summaries (
                    tenant_id TEXT NOT NULL DEFAULT 't_demo',
                    conversation_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    source_message_id INTEGER NOT NULL DEFAULT 0,
                    source_run_id TEXT,
                    generated_at TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, conversation_id)
                );
                -- M4（PRD 10.4）滚动摘要：编排侧周期生成、渠道层落库；下一轮随
                -- metadata 注入 understand/synthesize。与上面工作台的
                -- conversation_summaries 缓存是两回事。
                CREATE TABLE IF NOT EXISTS session_summaries (
                    tenant_id TEXT NOT NULL DEFAULT 't_demo',
                    conversation_id TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    upto_message_id INTEGER NOT NULL DEFAULT 0,
                    generated_at TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, conversation_id)
                );
                CREATE TABLE IF NOT EXISTS quick_replies (
                    tenant_id TEXT NOT NULL DEFAULT 't_demo',
                    reply_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    category TEXT,
                    created_by TEXT,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, reply_id)
                );
                """
            )
            _migrate(_conn)
            seed_identity_locked(_conn)
            seed_quick_replies_locked(_conn)
    return _conn


def _migrate(conn: sqlite3.Connection) -> None:
    """旧库迁移：补 tenant/身份/审计四要素列，并把旧数据归到默认租户。"""
    migrations = {
        "conversations": {
            "tenant_id": "TEXT NOT NULL DEFAULT 't_demo'",
            "assigned_agent_id": "TEXT",
            "customer_id": "TEXT",
            "state": "TEXT NOT NULL DEFAULT 'AI_ACTIVE'",
        },
        "messages": {"tenant_id": "TEXT NOT NULL DEFAULT 't_demo'"},
        "handoff_queue": {
            "tenant_id": "TEXT NOT NULL DEFAULT 't_demo'",
            "taken_by": "TEXT",
        },
        "assist_messages": {
            "tenant_id": "TEXT NOT NULL DEFAULT 't_demo'",
            "agent_id": "TEXT",
        },
    }
    for table, cols in migrations.items():
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col, ddl in cols.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _exec(sql: str, args: tuple = ()) -> None:
    with _lock, _get() as conn:
        conn.execute(sql, args)


def _query(sql: str, args: tuple = ()) -> list[sqlite3.Row]:
    with _lock:
        cur = _get().execute(sql, args)
        return cur.fetchall()


# ---------------------------------------------------------------------------
# 租户 / 用户 / 登录
# ---------------------------------------------------------------------------

_DEMO_USERS = (
    # tenant, user_id, username, role, display_name
    ("t_demo", "CU-1001", "customer", "customer", "王小明"),
    ("t_demo", "CU-1003", "customer3", "customer", "张店铺"),
    ("t_demo", "CU-1004", "customer4", "customer", "陈卖家"),
    ("t_demo", "AG-001", "agent", "agent", "客服小张"),
    ("t_demo", "SUP-001", "supervisor", "supervisor", "客服主管"),
    ("t_demo", "FIN-001", "finance", "finance", "财务李"),
    ("t_demo", "ADM-001", "admin", "admin", "平台管理员"),
    ("t_other", "CU-2001", "customer2", "customer", "第二租户客户"),
    ("t_other", "AG-002", "agent2", "agent", "第二租户坐席"),
    ("t_other", "SUP-002", "supervisor2", "supervisor", "第二租户主管"),
)


def seed_identity_locked(conn: sqlite3.Connection) -> None:
    """幂等 seed：两个租户 + 演示账号；密码 demo123（仅 dev）。"""
    now = _now()
    conn.execute(
        "INSERT OR IGNORE INTO tenants(tenant_id, name, plan, status, created_at) "
        "VALUES (?,?,?,?,?)",
        ("t_demo", "演示商家", "pro", "active", now),
    )
    conn.execute(
        "INSERT OR IGNORE INTO tenants(tenant_id, name, plan, status, created_at) "
        "VALUES (?,?,?,?,?)",
        ("t_other", "第二租户", "basic", "active", now),
    )
    # 演示账号的 scopes 与 ROLE_SCOPES 保持同一真相：seed 时刷新（不动密码）。
    for tenant, user_id, _username, role, display in _DEMO_USERS:
        scopes = json.dumps(sorted(ROLE_SCOPES.get(role, ())), ensure_ascii=False)
        conn.execute(
            "UPDATE users SET scopes=?, display_name=? "
            "WHERE tenant_id=? AND user_id=? AND (scopes<>? OR display_name<>?)",
            (scopes, display, tenant, user_id, scopes, display),
        )
    # 只为缺失用户计算一次密码哈希；已存在则跳过，避免每次连接都跑 PBKDF2
    existing = {
        (r["tenant_id"], r["username"])
        for r in conn.execute("SELECT tenant_id, username FROM users")
    }
    for tenant, user_id, username, role, display in _DEMO_USERS:
        if (tenant, username) in existing:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO users(tenant_id, user_id, username, password_hash, "
            "role, scopes, display_name, status, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                tenant, user_id, username,
                hash_password("demo123", iterations=50_000),
                role, json.dumps(list(ROLE_SCOPES.get(role, ())), ensure_ascii=False),
                display, "active", now,
            ),
        )


_QUICK_REPLIES = (
    ("QR-001", "物流查询", "已为您查询最新物流：{物流节点}。如需更详细的轨迹，我可以继续帮您查。", "物流"),
    ("QR-002", "对账说明", "对账单可在「财务对账」查看；如需盖章版或按账期拆分，请告诉我具体期间。", "财务"),
    ("QR-003", "售后流程", "售后流程：提交申请 → 资格/责任审核 → 寄回或上门 → 处理完成。当前进度我会持续同步。", "售后"),
    ("QR-004", "致歉转人工", "抱歉让您久等了，已为您转接人工客服，请稍候。", "通用"),
)


def seed_quick_replies_locked(conn: sqlite3.Connection) -> None:
    """幂等 seed：每个租户的快捷回复模板（可后续由主管维护）。"""
    now = _now()
    for tenant in ("t_demo", "t_other"):
        for reply_id, title, content, category in _QUICK_REPLIES:
            conn.execute(
                "INSERT OR IGNORE INTO quick_replies(tenant_id, reply_id, title, "
                "content, category, created_by, created_at) VALUES (?,?,?,?,?,?,?)",
                (tenant, reply_id, title, content, category, "system", now),
            )


def authenticate(tenant_id: str, username: str, password: str) -> dict | None:
    from shared.auth import verify_password

    rows = _query(
        "SELECT * FROM users WHERE tenant_id=? AND username=? AND status='active'",
        (tenant_id, username),
    )
    if not rows:
        return None
    user = dict(rows[0])
    if not verify_password(password, user["password_hash"]):
        return None
    user["scopes"] = json.loads(user["scopes"] or "[]")
    user.pop("password_hash", None)
    return user


def get_user(tenant_id: str, user_id: str) -> dict | None:
    rows = _query(
        "SELECT * FROM users WHERE tenant_id=? AND user_id=?", (tenant_id, user_id)
    )
    if not rows:
        return None
    user = dict(rows[0])
    user["scopes"] = json.loads(user["scopes"] or "[]")
    user.pop("password_hash", None)
    return user


def list_tenants() -> list[dict]:
    return [dict(r) for r in _query("SELECT tenant_id, name, plan, status FROM tenants")]


# ---------------------------------------------------------------------------
# conversations
# ---------------------------------------------------------------------------

def create_conversation(
    customer_id: str | None = None, *, tenant_id: str = DEFAULT_TENANT
) -> dict:
    conv_id = uuid.uuid4().hex[:16]
    _exec(
        "INSERT INTO conversations(id, tenant_id, mode, status, customer_id, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        (conv_id, tenant_id, "ai", "active", customer_id, _now(), _now()),
    )
    add_message(conv_id, "system", "会话已创建，AI 经营助手为您服务。", tenant_id=tenant_id)
    return get_conversation(conv_id, tenant_id=tenant_id)


def get_conversation(conv_id: str, *, tenant_id: str = DEFAULT_TENANT) -> dict | None:
    rows = _query(
        "SELECT * FROM conversations WHERE id=? AND tenant_id=?", (conv_id, tenant_id)
    )
    return dict(rows[0]) if rows else None


def update_conversation(
    conv_id: str, *, tenant_id: str = DEFAULT_TENANT, **fields
) -> None:
    fields["updated_at"] = _now()
    sets = ", ".join(f"{k}=?" for k in fields)
    _exec(
        f"UPDATE conversations SET {sets} WHERE id=? AND tenant_id=?",
        (*fields.values(), conv_id, tenant_id),
    )


def list_conversations(
    *, tenant_id: str = DEFAULT_TENANT, assigned_agent_id: str | None = None,
    customer_id: str | None = None, limit: int = 50,
) -> list[dict]:
    where = "c.tenant_id=?"
    args: list = [tenant_id]
    if customer_id:
        where += " AND c.customer_id=?"
        args.append(customer_id)
    if assigned_agent_id:
        # 坐席可见：未分配队列 + 自己接管的会话；主管/管理员不传此参数
        where += " AND (c.assigned_agent_id IS NULL OR c.assigned_agent_id=?)"
        args.append(assigned_agent_id)
    args.append(max(1, min(int(limit), 500)))
    rows = _query(
        f"""
        SELECT c.*, (
            SELECT content FROM messages m
            WHERE m.conversation_id = c.id AND m.tenant_id = c.tenant_id
            ORDER BY m.id DESC LIMIT 1
        ) AS last_message,
        (SELECT COUNT(*) FROM handoff_queue h
         WHERE h.conversation_id = c.id AND h.tenant_id = c.tenant_id
           AND h.status='pending') AS pending
        FROM conversations c WHERE {where}
        ORDER BY c.updated_at DESC, c.rowid DESC LIMIT ?
        """,
        tuple(args),
    )
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# messages
# ---------------------------------------------------------------------------

def add_message(
    conv_id: str, role: str, content: str, meta: dict | None = None,
    *, tenant_id: str = DEFAULT_TENANT
) -> dict:
    now = _now()
    _exec(
        "INSERT INTO messages(tenant_id, conversation_id, role, content, meta, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (tenant_id, conv_id, role, content,
         json.dumps(meta, ensure_ascii=False) if meta else None, now),
    )
    update_conversation(conv_id, tenant_id=tenant_id, updated_at=now)
    row = dict(_query(
        "SELECT * FROM messages WHERE conversation_id=? AND tenant_id=? "
        "ORDER BY id DESC LIMIT 1",
        (conv_id, tenant_id),
    )[0])
    # 与 list_messages/get_message 一致：WS 广播直接带 dict，前端才能读 meta.cards
    row["meta"] = json.loads(row["meta"]) if row.get("meta") else None
    return row


def list_messages(
    conv_id: str, after_id: int = 0, *, tenant_id: str = DEFAULT_TENANT
) -> list[dict]:
    rows = _query(
        "SELECT * FROM messages WHERE conversation_id=? AND tenant_id=? AND id>? "
        "ORDER BY id",
        (conv_id, tenant_id, after_id),
    )
    out = []
    for r in rows:
        d = dict(r)
        d["meta"] = json.loads(d["meta"]) if d["meta"] else None
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# handoff queue
# ---------------------------------------------------------------------------

def enqueue_handoff(package: dict, *, tenant_id: str = DEFAULT_TENANT) -> int:
    _exec(
        "INSERT INTO handoff_queue(tenant_id, conversation_id, trace_id, reason, "
        "package, created_at) VALUES (?,?,?,?,?,?)",
        (
            tenant_id,
            package.get("context_id", ""),
            package.get("trace_id", ""),
            "；".join(package.get("reasons", [])),
            json.dumps(package, ensure_ascii=False),
            _now(),
        ),
    )
    row = _query(
        "SELECT MAX(id) AS id FROM handoff_queue WHERE tenant_id=?", (tenant_id,)
    )[0]
    return int(row["id"])


def get_handoff(
    conv_id: str, *, tenant_id: str = DEFAULT_TENANT
) -> dict | None:
    rows = _query(
        "SELECT * FROM handoff_queue WHERE conversation_id=? AND tenant_id=? "
        "ORDER BY id DESC LIMIT 1",
        (conv_id, tenant_id),
    )
    if not rows:
        return None
    d = dict(rows[0])
    d["package"] = json.loads(d["package"])
    return d


def take_handoff(
    conv_id: str, *, tenant_id: str = DEFAULT_TENANT, taken_by: str = ""
) -> bool:
    row = get_handoff(conv_id, tenant_id=tenant_id)
    if not row or row["status"] != "pending":
        return False
    _exec(
        "UPDATE handoff_queue SET status='taken', taken_by=? "
        "WHERE id=? AND tenant_id=?",
        (taken_by, row["id"], tenant_id),
    )
    return True


# ---------------------------------------------------------------------------
# 坐席 AI 助手
# ---------------------------------------------------------------------------

def add_assist_message(
    role: str, content: str, *, tenant_id: str = DEFAULT_TENANT, agent_id: str = ""
) -> dict:
    now = _now()
    _exec(
        "INSERT INTO assist_messages(tenant_id, agent_id, role, content, created_at) "
        "VALUES (?,?,?,?,?)",
        (tenant_id, agent_id, role, content, now),
    )
    row = _query(
        "SELECT * FROM assist_messages WHERE tenant_id=? ORDER BY id DESC LIMIT 1",
        (tenant_id,),
    )[0]
    return dict(row)


def list_assist(*, tenant_id: str = DEFAULT_TENANT, limit: int = 50) -> list[dict]:
    rows = _query(
        "SELECT * FROM assist_messages WHERE tenant_id=? ORDER BY id DESC LIMIT ?",
        (tenant_id, limit),
    )
    return [dict(r) for r in reversed(rows)]


# ---------------------------------------------------------------------------
# 1B：待确认/待审批动作（会话级一等状态）
# ---------------------------------------------------------------------------

def create_pending_action(
    conversation_id: str,
    *,
    type: str,  # noqa: A002
    payload: dict,
    tenant_id: str = DEFAULT_TENANT,
    domain: str = "",
    skill: str = "",
    preview: dict | None = None,
    confirm_token: str = "",
    approval_id: str = "",
    expires_at: str = "",
    trace_id: str = "",
) -> dict:
    action_id = uuid.uuid4().hex
    now = _now()
    _exec(
        "INSERT INTO pending_actions(action_id, tenant_id, conversation_id, trace_id, "
        "type, domain, skill, payload, preview, confirm_token, approval_id, status, "
        "expires_at, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            action_id, tenant_id, conversation_id, trace_id, type, domain, skill,
            json.dumps(payload, ensure_ascii=False),
            json.dumps(preview or {}, ensure_ascii=False),
            confirm_token or "", approval_id or "", "pending", expires_at or "",
            now, now,
        ),
    )
    return get_pending_action(action_id, tenant_id=tenant_id) or {}


def get_pending_action(action_id: str, *, tenant_id: str = DEFAULT_TENANT) -> dict | None:
    rows = _query(
        "SELECT * FROM pending_actions WHERE action_id=? AND tenant_id=?",
        (action_id, tenant_id),
    )
    if not rows:
        return None
    d = dict(rows[0])
    for key in ("payload", "preview"):
        try:
            d[key] = json.loads(d[key]) if d.get(key) else {}
        except json.JSONDecodeError:
            d[key] = {}
    return d


def list_pending_actions(
    conversation_id: str, *, tenant_id: str = DEFAULT_TENANT,
    status: str = "pending",
) -> list[dict]:
    rows = _query(
        "SELECT * FROM pending_actions WHERE tenant_id=? AND conversation_id=? "
        "AND status=? ORDER BY created_at DESC",
        (tenant_id, conversation_id, status),
    )
    out = []
    for r in rows:
        d = dict(r)
        for key in ("payload", "preview"):
            try:
                d[key] = json.loads(d[key]) if d.get(key) else {}
            except json.JSONDecodeError:
                d[key] = {}
        out.append(d)
    return out


def get_pending_action_by_approval(
    approval_id: str, *, tenant_id: str = DEFAULT_TENANT
) -> dict | None:
    rows = _query(
        "SELECT * FROM pending_actions WHERE tenant_id=? AND approval_id=? "
        "ORDER BY created_at DESC LIMIT 1",
        (tenant_id, approval_id),
    )
    if not rows:
        return None
    d = dict(rows[0])
    for key in ("payload", "preview"):
        try:
            d[key] = json.loads(d[key]) if d.get(key) else {}
        except json.JSONDecodeError:
            d[key] = {}
    return d


def update_pending_action_status(
    action_id: str, status: str, *, tenant_id: str = DEFAULT_TENANT
) -> None:
    _exec(
        "UPDATE pending_actions SET status=?, updated_at=? "
        "WHERE action_id=? AND tenant_id=?",
        (status, _now(), action_id, tenant_id),
    )


# ---------------------------------------------------------------------------
# 2A：agent_runs / agent_steps（编排调用链落库）
# ---------------------------------------------------------------------------

def record_agent_run(
    conversation_id: str,
    *,
    tenant_id: str,
    trace_id: str = "",
    intent: str = "",
    model: str = "",
    prompt_version: str = "",
    status: str = "completed",
    error: str = "",
    started_at: str = "",
    steps: list[dict] | None = None,
) -> dict | None:
    run_id = uuid.uuid4().hex
    finished = _now()
    with _lock, _get() as conn:
        conn.execute(
            "INSERT INTO agent_runs(run_id, tenant_id, conversation_id, trace_id, "
            "intent, model, prompt_version, status, error, started_at, finished_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, tenant_id, conversation_id, trace_id, intent, model,
             prompt_version, status, error, started_at or finished, finished),
        )
        for seq, step in enumerate(steps or [], start=1):
            conn.execute(
                "INSERT INTO agent_steps(run_id, tenant_id, conversation_id, seq, "
                "stage, domain, tool, input_summary, output_summary, latency_ms, "
                "status, error, prompt_version, model, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id, tenant_id, conversation_id, seq,
                    str(step.get("stage") or "unknown"),
                    str(step.get("domain") or ""),
                    str(step.get("tool") or step.get("skill") or ""),
                    str(step.get("input_summary") or "")[:300],
                    str(step.get("output_summary") or "")[:400],
                    step.get("latency_ms"),
                    str(step.get("status") or "completed"),
                    str(step.get("error") or "")[:200],
                    str(step.get("prompt_version") or prompt_version)[:40],
                    str(step.get("model") or model)[:60],
                    finished,
                ),
            )
    return get_agent_run(run_id, tenant_id=tenant_id)


def get_agent_run(run_id: str, *, tenant_id: str = DEFAULT_TENANT) -> dict | None:
    runs = _query(
        "SELECT * FROM agent_runs WHERE run_id=? AND tenant_id=?",
        (run_id, tenant_id),
    )
    if not runs:
        return None
    run = dict(runs[0])
    run["steps"] = [
        dict(r) for r in _query(
            "SELECT * FROM agent_steps WHERE run_id=? AND tenant_id=? ORDER BY seq",
            (run_id, tenant_id),
        )
    ]
    return run


def list_agent_runs(
    conversation_id: str, *, tenant_id: str = DEFAULT_TENANT, limit: int = 20
) -> list[dict]:
    runs = _query(
        "SELECT * FROM agent_runs WHERE tenant_id=? AND conversation_id=? "
        "ORDER BY finished_at DESC LIMIT ?",
        (tenant_id, conversation_id, limit),
    )
    out = []
    for r in runs:
        run = dict(r)
        run["steps"] = [
            dict(s) for s in _query(
                "SELECT * FROM agent_steps WHERE run_id=? AND tenant_id=? ORDER BY seq",
                (run["run_id"], tenant_id),
            )
        ]
        out.append(run)
    return out


# ---------------------------------------------------------------------------
# 2B：附件 / feedback
# ---------------------------------------------------------------------------

def create_attachment(
    *,
    attachment_id: str,
    tenant_id: str,
    uploader_id: str,
    filename: str,
    mime: str,
    size: int,
    sha256: str,
    storage_path: str,
    conversation_id: str = "",
    message_id: int | None = None,
) -> dict:
    now = _now()
    _exec(
        "INSERT INTO attachments(attachment_id, tenant_id, conversation_id, "
        "message_id, uploader_id, filename, mime, size, sha256, storage_path, "
        "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (attachment_id, tenant_id, conversation_id, message_id, uploader_id,
         filename, mime, size, sha256, storage_path, now),
    )
    return get_attachment(attachment_id, tenant_id=tenant_id) or {}


def get_attachment(
    attachment_id: str, *, tenant_id: str = DEFAULT_TENANT
) -> dict | None:
    rows = _query(
        "SELECT * FROM attachments WHERE attachment_id=? AND tenant_id=?",
        (attachment_id, tenant_id),
    )
    return dict(rows[0]) if rows else None


def bind_attachment(
    attachment_id: str, message_id: int, conversation_id: str, *,
    tenant_id: str = DEFAULT_TENANT,
) -> None:
    _exec(
        "UPDATE attachments SET message_id=?, conversation_id=? "
        "WHERE attachment_id=? AND tenant_id=?",
        (message_id, conversation_id, attachment_id, tenant_id),
    )


def create_feedback(
    conversation_id: str, message_id: int, reason: str, *,
    tenant_id: str, created_by: str,
    agent_answer: str = "", edited_answer: str = "",
) -> dict:
    _exec(
        "INSERT INTO feedback(tenant_id, conversation_id, message_id, "
        "agent_answer, edited_answer, reason, created_by, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (tenant_id, conversation_id, message_id, agent_answer, edited_answer,
         reason, created_by, _now()),
    )
    rows = _query(
        "SELECT * FROM feedback WHERE tenant_id=? AND conversation_id=? "
        "ORDER BY id DESC LIMIT 1",
        (tenant_id, conversation_id),
    )
    return dict(rows[0]) if rows else {}


def get_message(
    message_id: int, *, tenant_id: str = DEFAULT_TENANT
) -> dict | None:
    rows = _query(
        "SELECT * FROM messages WHERE id=? AND tenant_id=?",
        (message_id, tenant_id),
    )
    if not rows:
        return None
    d = dict(rows[0])
    d["meta"] = json.loads(d["meta"]) if d.get("meta") else None
    return d


def set_message_meta(
    message_id: int, meta: dict | None, *, tenant_id: str = DEFAULT_TENANT
) -> None:
    _exec(
        "UPDATE messages SET meta=? WHERE id=? AND tenant_id=?",
        (json.dumps(meta, ensure_ascii=False) if meta else None,
         message_id, tenant_id),
    )


# ---------------------------------------------------------------------------
# Phase 4：工作台（备注 / 已读 / 摘要缓存 / 快捷回复）
# ---------------------------------------------------------------------------

def add_note(
    conversation_id: str, content: str, *, tenant_id: str, author_id: str,
) -> dict:
    _exec(
        "INSERT INTO conversation_notes(tenant_id, conversation_id, author_id, "
        "content, created_at) VALUES (?,?,?,?,?)",
        (tenant_id, conversation_id, author_id, content, _now()),
    )
    rows = _query(
        "SELECT * FROM conversation_notes WHERE tenant_id=? AND conversation_id=? "
        "ORDER BY id DESC LIMIT 1",
        (tenant_id, conversation_id),
    )
    return dict(rows[0]) if rows else {}


def list_notes(
    conversation_id: str, *, tenant_id: str = DEFAULT_TENANT, limit: int = 100,
) -> list[dict]:
    rows = _query(
        "SELECT * FROM conversation_notes WHERE tenant_id=? AND conversation_id=? "
        "ORDER BY id DESC LIMIT ?",
        (tenant_id, conversation_id, max(1, min(int(limit), 200))),
    )
    return [dict(r) for r in rows]


def mark_conversation_read(
    conversation_id: str, user_id: str, last_message_id: int, *,
    tenant_id: str = DEFAULT_TENANT,
) -> int:
    _exec(
        "INSERT INTO conversation_reads(tenant_id, conversation_id, user_id, "
        "last_read_message_id, updated_at) VALUES (?,?,?,?,?) "
        "ON CONFLICT(tenant_id, conversation_id, user_id) DO UPDATE SET "
        "last_read_message_id=MAX(last_read_message_id, excluded.last_read_message_id), "
        "updated_at=excluded.updated_at",
        (tenant_id, conversation_id, user_id, int(last_message_id), _now()),
    )
    row = _query(
        "SELECT last_read_message_id FROM conversation_reads "
        "WHERE tenant_id=? AND conversation_id=? AND user_id=?",
        (tenant_id, conversation_id, user_id),
    )
    return int(row[0]["last_read_message_id"]) if row else 0


def get_last_read(conversation_id: str, user_id: str, *,
                  tenant_id: str = DEFAULT_TENANT) -> int:
    rows = _query(
        "SELECT last_read_message_id FROM conversation_reads "
        "WHERE tenant_id=? AND conversation_id=? AND user_id=?",
        (tenant_id, conversation_id, user_id),
    )
    return int(rows[0]["last_read_message_id"]) if rows else 0


def count_unread(
    conversation_id: str, user_id: str, *, tenant_id: str = DEFAULT_TENANT,
) -> int:
    last = get_last_read(conversation_id, user_id, tenant_id=tenant_id)
    rows = _query(
        "SELECT COUNT(*) AS n FROM messages WHERE tenant_id=? AND conversation_id=? "
        "AND role='customer' AND id>?",
        (tenant_id, conversation_id, last),
    )
    return int(rows[0]["n"]) if rows else 0


def max_message_id(conversation_id: str, *, tenant_id: str = DEFAULT_TENANT) -> int:
    rows = _query(
        "SELECT COALESCE(MAX(id), 0) AS n FROM messages "
        "WHERE tenant_id=? AND conversation_id=?",
        (tenant_id, conversation_id),
    )
    return int(rows[0]["n"]) if rows else 0


def get_summary_cache(
    conversation_id: str, *, tenant_id: str = DEFAULT_TENANT,
) -> dict | None:
    rows = _query(
        "SELECT * FROM conversation_summaries WHERE tenant_id=? AND conversation_id=?",
        (tenant_id, conversation_id),
    )
    if not rows:
        return None
    d = dict(rows[0])
    try:
        d["payload"] = json.loads(d["payload"]) if d.get("payload") else {}
    except json.JSONDecodeError:
        d["payload"] = {}
    return d


def upsert_summary_cache(
    conversation_id: str, payload: dict, *, tenant_id: str,
    source_message_id: int = 0, source_run_id: str = "",
) -> dict:
    now = _now()
    _exec(
        "INSERT INTO conversation_summaries(tenant_id, conversation_id, payload, "
        "source_message_id, source_run_id, generated_at) VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(tenant_id, conversation_id) DO UPDATE SET "
        "payload=excluded.payload, source_message_id=excluded.source_message_id, "
        "source_run_id=excluded.source_run_id, generated_at=excluded.generated_at",
        (tenant_id, conversation_id, json.dumps(payload, ensure_ascii=False),
         int(source_message_id), source_run_id or "", now),
    )
    return get_summary_cache(conversation_id, tenant_id=tenant_id) or {}


# ---------------------------------------------------------------------------
# session summaries（M4 / PRD 10.4：滚动摘要，编排生成 → 渠道层落库）
# ---------------------------------------------------------------------------

def get_session_summary(
    conversation_id: str, *, tenant_id: str = DEFAULT_TENANT,
) -> dict | None:
    rows = _query(
        "SELECT * FROM session_summaries WHERE tenant_id=? AND conversation_id=?",
        (tenant_id, conversation_id),
    )
    return dict(rows[0]) if rows else None


def upsert_session_summary(
    conversation_id: str, summary: str, upto_message_id: int, *,
    tenant_id: str = DEFAULT_TENANT,
) -> dict:
    now = _now()
    _exec(
        "INSERT INTO session_summaries(tenant_id, conversation_id, summary, "
        "upto_message_id, generated_at) VALUES (?,?,?,?,?) "
        "ON CONFLICT(tenant_id, conversation_id) DO UPDATE SET "
        "summary=excluded.summary, upto_message_id=excluded.upto_message_id, "
        "generated_at=excluded.generated_at",
        (tenant_id, conversation_id, summary, int(upto_message_id), now),
    )
    return get_session_summary(conversation_id, tenant_id=tenant_id) or {}


def list_quick_replies(*, tenant_id: str = DEFAULT_TENANT) -> list[dict]:
    rows = _query(
        "SELECT reply_id, title, content, category FROM quick_replies "
        "WHERE tenant_id=? ORDER BY reply_id",
        (tenant_id,),
    )
    return [dict(r) for r in rows]


def last_message(
    conversation_id: str, *, tenant_id: str = DEFAULT_TENANT,
) -> dict | None:
    rows = _query(
        "SELECT * FROM messages WHERE tenant_id=? AND conversation_id=? "
        "ORDER BY id DESC LIMIT 1",
        (tenant_id, conversation_id),
    )
    if not rows:
        return None
    d = dict(rows[0])
    d["meta"] = json.loads(d["meta"]) if d.get("meta") else None
    return d


def list_users(
    *, tenant_id: str = DEFAULT_TENANT, roles: tuple[str, ...] | None = None,
) -> list[dict]:
    sql = ("SELECT user_id, username, role, display_name, status, scopes "
           "FROM users WHERE tenant_id=?")
    args: list = [tenant_id]
    if roles:
        sql += " AND role IN (" + ",".join("?" for _ in roles) + ")"
        args.extend(roles)
    sql += " ORDER BY role, user_id"
    rows = _query(sql, tuple(args))
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["scopes"] = json.loads(d.get("scopes") or "[]")
        except json.JSONDecodeError:
            d["scopes"] = []
        out.append(d)
    return out


def assignment_counts(*, tenant_id: str = DEFAULT_TENANT) -> dict[str, int]:
    rows = _query(
        "SELECT assigned_agent_id, COUNT(*) AS n FROM conversations "
        "WHERE tenant_id=? AND state<>'CLOSED' AND assigned_agent_id IS NOT NULL "
        "GROUP BY assigned_agent_id",
        (tenant_id,),
    )
    return {r["assigned_agent_id"]: int(r["n"]) for r in rows}
