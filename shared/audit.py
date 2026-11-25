"""统一审计事件（哈希链 + HMAC + 每租户 head，append-only）。

1B 起：
- `SECURITY_MODE=remote` 时所有写/读都走 securityd（单写者）；
- local 模式只用于单进程测试，SQLite 写用 BEGIN IMMEDIATE + 进程内锁；
- 每条事件有 event_id（幂等）、seq（租户内递增）、prev_hash、entry_hash、
  signature（HMAC-SHA256）；audit_heads 记录每租户链尾，能发现链尾删除。
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from typing import Any

from shared.auth import AuthContext, auth_from_config
from shared.config import get_settings
from shared.keys import get_audit_signing_key, sign_hmac, verify_hmac
from shared.pii import mask_obj
from shared.security_db import _lock, get_conn, now

logger = logging.getLogger(__name__)

_GENESIS = "0" * 64
_EVENT_FIELDS = (
    "tenant_id", "actor_id", "actor_role", "action", "target", "params",
    "result_summary", "trace_id", "context_id", "task_id", "ip", "user_agent",
    "domain", "latency_ms", "status", "error", "prompt_version", "model",
    "created_at",
)


def _hash(prev: str, row: dict[str, Any]) -> str:
    payload = json.dumps(
        {"prev": prev, **{k: row.get(k, "") or "" for k in _EVENT_FIELDS}},
        sort_keys=True, ensure_ascii=False, default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _sign(entry_hash: str) -> str:
    return sign_hmac(get_audit_signing_key(), entry_hash.encode("utf-8"))


def _ensure() -> None:
    conn = get_conn()
    with _lock, conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(audit_events)")}
        if cols and "signature" not in cols:
            _migrate_legacy(conn, cols)
            cols = {r[1] for r in conn.execute("PRAGMA table_info(audit_events)")}
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                seq INTEGER NOT NULL,
                tenant_id TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                actor_role TEXT NOT NULL,
                action TEXT NOT NULL,
                target TEXT,
                params TEXT,
                result_summary TEXT,
                trace_id TEXT,
                context_id TEXT,
                task_id TEXT,
                ip TEXT,
                user_agent TEXT,
                domain TEXT,
                latency_ms INTEGER,
                status TEXT,
                error TEXT,
                prompt_version TEXT,
                model TEXT,
                created_at TEXT NOT NULL,
                prev_hash TEXT NOT NULL,
                entry_hash TEXT NOT NULL,
                signature TEXT NOT NULL,
                UNIQUE (tenant_id, seq)
            );
            CREATE INDEX IF NOT EXISTS idx_audit_tenant_seq
                ON audit_events(tenant_id, seq);
            CREATE INDEX IF NOT EXISTS idx_audit_action
                ON audit_events(action, id);
            CREATE TABLE IF NOT EXISTS audit_heads (
                tenant_id TEXT PRIMARY KEY,
                last_seq INTEGER NOT NULL,
                last_hash TEXT NOT NULL,
                count INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )


def _migrate_legacy(conn, cols: set[str]) -> None:
    """旧表（无 event_id/seq/signature）重建并按租户重放哈希链。"""
    old = "audit_events_legacy"
    conn.execute(f"DROP TABLE IF EXISTS {old}")
    conn.execute(f"ALTER TABLE audit_events RENAME TO {old}")
    conn.execute(
        """
        CREATE TABLE audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE,
            seq INTEGER NOT NULL,
            tenant_id TEXT NOT NULL,
            actor_id TEXT NOT NULL,
            actor_role TEXT NOT NULL,
            action TEXT NOT NULL,
            target TEXT, params TEXT, result_summary TEXT,
            trace_id TEXT, context_id TEXT, task_id TEXT, ip TEXT, user_agent TEXT,
            domain TEXT, latency_ms INTEGER, status TEXT, error TEXT,
            prompt_version TEXT, model TEXT,
            created_at TEXT NOT NULL, prev_hash TEXT NOT NULL,
            entry_hash TEXT NOT NULL, signature TEXT NOT NULL,
            UNIQUE (tenant_id, seq)
        )
        """
    )
    rows = conn.execute(f"SELECT * FROM {old} ORDER BY id").fetchall()
    heads: dict[str, tuple[int, str, int]] = {}
    for row in rows:
        d = dict(row)
        tenant = str(d.get("tenant_id") or "unknown")
        seq, prev, count = heads.get(tenant, (0, _GENESIS, 0))
        seq += 1
        d.setdefault("domain", "")
        d.setdefault("latency_ms", None)
        d.setdefault("status", "")
        d.setdefault("error", "")
        d.setdefault("prompt_version", "")
        d.setdefault("model", "")
        entry = _hash(prev, d)
        sig = _sign(entry)
        conn.execute(
            "INSERT INTO audit_events(event_id, seq, tenant_id, actor_id, actor_role, "
            "action, target, params, result_summary, trace_id, context_id, task_id, ip, "
            "user_agent, domain, latency_ms, status, error, prompt_version, model, "
            "created_at, prev_hash, entry_hash, signature) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"legacy-{d.get('id')}", seq, tenant, d.get("actor_id") or "anonymous",
                d.get("actor_role") or "anonymous", d.get("action") or "unknown",
                d.get("target") or "", d.get("params") or "",
                d.get("result_summary") or "", d.get("trace_id") or "",
                d.get("context_id") or "", d.get("task_id") or "", d.get("ip") or "",
                d.get("user_agent") or "", d.get("domain") or "",
                d.get("latency_ms"), d.get("status") or "", d.get("error") or "",
                d.get("prompt_version") or "", d.get("model") or "",
                d.get("created_at") or now(), prev, entry, sig,
            ),
        )
        heads[tenant] = (seq, entry, count + 1)
    conn.execute(f"DROP TABLE {old}")
    conn.execute("DELETE FROM audit_heads")
    for tenant, (seq, entry, count) in heads.items():
        conn.execute(
            "INSERT INTO audit_heads(tenant_id, last_seq, last_hash, count, updated_at) "
            "VALUES (?,?,?,?,?)",
            (tenant, seq, entry, count, now()),
        )


def _remote() -> bool:
    return get_settings().security_mode == "remote"


def _payload(
    *, tenant_id: str, actor_id: str, actor_role: str, action: str,
    target: str = "", params: dict | None = None, result_summary: str = "",
    trace_id: str = "", context_id: str = "", task_id: str = "",
    ip: str = "", user_agent: str = "", event_id: str = "",
    domain: str = "", latency_ms: int | None = None, status: str = "",
    error: str = "", prompt_version: str = "", model: str = "",
    created_at: str = "",
) -> dict:
    safe_params = json.dumps(mask_obj(params or {}), ensure_ascii=False)
    return {
        "event_id": event_id or uuid.uuid4().hex,
        "tenant_id": tenant_id,
        "actor_id": actor_id or "anonymous",
        "actor_role": actor_role or "anonymous",
        "action": action,
        "target": target or "",
        "params": safe_params,
        "result_summary": mask_obj(str(result_summary))[:500],
        "trace_id": trace_id or "",
        "context_id": context_id or "",
        "task_id": task_id or "",
        "ip": ip or "",
        "user_agent": user_agent or "",
        "domain": domain or "",
        "latency_ms": int(latency_ms) if latency_ms is not None else None,
        "status": status or "",
        "error": error or "",
        "prompt_version": prompt_version or "",
        "model": model or "",
        "created_at": created_at or now(),
    }


def audit_event(
    *,
    tenant_id: str,
    actor_id: str,
    actor_role: str,
    action: str,
    target: str = "",
    params: dict | None = None,
    result_summary: str = "",
    trace_id: str = "",
    context_id: str = "",
    task_id: str = "",
    ip: str = "",
    user_agent: str = "",
    strict: bool = False,
    event_id: str = "",
    domain: str = "",
    latency_ms: int | None = None,
    status: str = "",
    error: str = "",
    prompt_version: str = "",
    model: str = "",
    created_at: str = "",
) -> int | None:
    """写一条审计；strict=True 时错误向上抛（高价值动作必须留痕）。"""
    row = _payload(
        tenant_id=tenant_id, actor_id=actor_id, actor_role=actor_role,
        action=action, target=target, params=params, result_summary=result_summary,
        trace_id=trace_id, context_id=context_id, task_id=task_id, ip=ip,
        user_agent=user_agent, event_id=event_id, domain=domain,
        latency_ms=latency_ms, status=status, error=error,
        prompt_version=prompt_version, model=model, created_at=created_at,
    )
    if _remote():
        try:
            from shared.security_client import get_client

            return get_client().audit_append(row)
        except Exception:  # noqa: BLE001
            logger.exception("audit_event remote failed action=%s", action)
            if strict:
                raise
            return None

    try:
        _ensure()
        conn = get_conn()
        with _lock:
            if conn.in_transaction:
                conn.commit()
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = conn.execute(
                    "SELECT id FROM audit_events WHERE event_id=?",
                    (row["event_id"],),
                ).fetchone()
                if existing:
                    conn.rollback()
                    return int(existing["id"])
                head = conn.execute(
                    "SELECT last_seq, last_hash, count FROM audit_heads "
                    "WHERE tenant_id=?",
                    (tenant_id,),
                ).fetchone()
                last_seq = int(head["last_seq"]) if head else 0
                prev = str(head["last_hash"]) if head else _GENESIS
                seq = last_seq + 1
                entry = _hash(prev, row)
                sig = _sign(entry)
                cur = conn.execute(
                    "INSERT INTO audit_events(event_id, seq, tenant_id, actor_id, "
                    "actor_role, action, target, params, result_summary, trace_id, "
                    "context_id, task_id, ip, user_agent, domain, latency_ms, status, "
                    "error, prompt_version, model, created_at, prev_hash, entry_hash, "
                    "signature) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        row["event_id"], seq, tenant_id, row["actor_id"],
                        row["actor_role"], action, row["target"], row["params"],
                        row["result_summary"], row["trace_id"], row["context_id"],
                        row["task_id"], row["ip"], row["user_agent"], row["domain"],
                        row["latency_ms"], row["status"], row["error"],
                        row["prompt_version"], row["model"], row["created_at"],
                        prev, entry, sig,
                    ),
                )
                conn.execute(
                    "INSERT INTO audit_heads(tenant_id, last_seq, last_hash, count, "
                    "updated_at) VALUES (?,?,?,?,?) ON CONFLICT(tenant_id) DO UPDATE SET "
                    "last_seq=excluded.last_seq, last_hash=excluded.last_hash, "
                    "count=excluded.count, updated_at=excluded.updated_at",
                    (tenant_id, seq, entry, (int(head["count"]) if head else 0) + 1,
                     row["created_at"]),
                )
                conn.commit()
                return int(cur.lastrowid)
            except Exception:
                conn.rollback()
                raise
    except Exception:  # noqa: BLE001
        logger.exception("audit_event failed action=%s target=%s", action, target)
        if strict:
            raise
        return None


def audit_from_auth(
    auth: AuthContext | None,
    action: str,
    *,
    target: str = "",
    params: dict | None = None,
    result_summary: str = "",
    trace_id: str = "",
    context_id: str = "",
    task_id: str = "",
    ip: str = "",
    user_agent: str = "",
    strict: bool = False,
    domain: str = "",
    latency_ms: int | None = None,
    status: str = "",
    error: str = "",
    prompt_version: str = "",
    model: str = "",
) -> int | None:
    if auth is None:
        return audit_event(
            tenant_id="unknown", actor_id="anonymous", actor_role="anonymous",
            action=action, target=target, params=params,
            result_summary=result_summary, trace_id=trace_id,
            context_id=context_id, task_id=task_id, ip=ip, user_agent=user_agent,
            strict=strict, domain=domain, latency_ms=latency_ms, status=status,
            error=error, prompt_version=prompt_version, model=model,
        )
    return audit_event(
        tenant_id=auth.tenant_id, actor_id=auth.actor_id, actor_role=auth.role,
        action=action, target=target, params=params,
        result_summary=result_summary, trace_id=trace_id, context_id=context_id,
        task_id=task_id, ip=ip, user_agent=user_agent, strict=strict,
        domain=domain, latency_ms=latency_ms, status=status, error=error,
        prompt_version=prompt_version, model=model,
    )


def audit_from_config(
    config: dict | None,
    action: str,
    *,
    target: str = "",
    params: dict | None = None,
    result_summary: str = "",
    trace_id: str = "",
    context_id: str = "",
    task_id: str = "",
    strict: bool = False,
    domain: str = "",
    latency_ms: int | None = None,
    status: str = "",
    error: str = "",
    prompt_version: str = "",
    model: str = "",
) -> int | None:
    return audit_from_auth(
        auth_from_config(config), action, target=target, params=params,
        result_summary=result_summary, trace_id=trace_id,
        context_id=context_id, task_id=task_id, strict=strict,
        domain=domain, latency_ms=latency_ms, status=status, error=error,
        prompt_version=prompt_version, model=model,
    )


def list_events(
    tenant_id: str | None = None, limit: int = 200, *,
    trace_id: str = "", context_id: str = "", action: str = "",
) -> list[dict]:
    if _remote():
        from shared.security_client import get_client

        return get_client().audit_list(
            tenant_id=tenant_id or "", limit=limit, trace_id=trace_id,
            context_id=context_id, action=action,
        )
    _ensure()
    sql = "SELECT * FROM audit_events WHERE 1=1"
    args: list[Any] = []
    if tenant_id:
        sql += " AND tenant_id=?"
        args.append(tenant_id)
    if trace_id:
        sql += " AND trace_id=?"
        args.append(trace_id)
    if context_id:
        sql += " AND context_id=?"
        args.append(context_id)
    if action:
        sql += " AND action=?"
        args.append(action)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with _lock:
        rows = get_conn().execute(sql, tuple(args)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["params"] = json.loads(d["params"]) if d.get("params") else {}
        except json.JSONDecodeError:
            pass
        out.append(d)
    return out


def verify_chain(tenant_id: str | None = None) -> dict:
    """校验哈希链 + HMAC + 每租户 head（能发现链尾删除）。"""
    if _remote():
        from shared.security_client import get_client

        return get_client().audit_verify(tenant_id)
    _ensure()
    with _lock:
        if tenant_id:
            rows = get_conn().execute(
                "SELECT * FROM audit_events WHERE tenant_id=? ORDER BY tenant_id, seq",
                (tenant_id,),
            ).fetchall()
            heads = get_conn().execute(
                "SELECT * FROM audit_heads WHERE tenant_id=?", (tenant_id,)
            ).fetchall()
        else:
            rows = get_conn().execute(
                "SELECT * FROM audit_events ORDER BY tenant_id, seq"
            ).fetchall()
            heads = get_conn().execute("SELECT * FROM audit_heads").fetchall()
    prev_by_tenant: dict[str, str] = {}
    seq_by_tenant: dict[str, int] = {}
    count_by_tenant: dict[str, int] = {}
    last_hash: dict[str, str] = {}
    for r in rows:
        d = dict(r)
        tenant = d["tenant_id"]
        prev = prev_by_tenant.get(tenant, _GENESIS)
        seq = seq_by_tenant.get(tenant, 0) + 1
        rid = d["id"]
        if int(d.get("seq") or 0) != seq:
            return {"ok": False, "total": len(rows), "broken_at": rid,
                    "reason": f"第 {rid} 条 seq 不连续（第 {seq} 条应为 {d.get('seq')}）"}
        if d["prev_hash"] != prev:
            return {"ok": False, "total": len(rows), "broken_at": rid,
                    "reason": f"第 {rid} 条 prev_hash 断链（前一条被删除或篡改）"}
        if d["entry_hash"] != _hash(prev, d):
            return {"ok": False, "total": len(rows), "broken_at": rid,
                    "reason": f"第 {rid} 条内容与哈希不符（被篡改）"}
        if not verify_hmac(
            get_audit_signing_key(), d["entry_hash"].encode("utf-8"),
            str(d.get("signature") or ""),
        ):
            return {"ok": False, "total": len(rows), "broken_at": rid,
                    "reason": f"第 {rid} 条 HMAC 签名无效"}
        prev_by_tenant[tenant] = d["entry_hash"]
        seq_by_tenant[tenant] = seq
        count_by_tenant[tenant] = count_by_tenant.get(tenant, 0) + 1
        last_hash[tenant] = d["entry_hash"]

    head_by_tenant = {h["tenant_id"]: dict(h) for h in heads}
    for tenant, count in count_by_tenant.items():
        head = head_by_tenant.get(tenant)
        if head is None:
            return {"ok": False, "total": len(rows), "broken_at": None,
                    "reason": f"租户 {tenant} 缺少 head 锚点"}
        if int(head["count"]) != count or int(head["last_seq"]) != seq_by_tenant[tenant] \
                or head["last_hash"] != last_hash[tenant]:
            return {"ok": False, "total": len(rows), "broken_at": None,
                    "reason": f"租户 {tenant} 链尾/计数与 head 不一致（可能删除链尾）"}
    for tenant, head in head_by_tenant.items():
        if tenant not in count_by_tenant and int(head["count"]) != 0:
            return {"ok": False, "total": len(rows), "broken_at": None,
                    "reason": f"租户 {tenant} 有 head 但事件为空（链尾被删除）"}
    return {"ok": True, "total": len(rows), "broken_at": None,
            "reason": None if rows else "审计日志为空"}


def audited_tool_node(tools: list, domain: str, tool_to_skill: dict[str, str]):
    """逐工具执行 + 统一审计 + tool 事件（tool 级 trace 数据源）。

    1B 起支持从 request context 注入 confirm_token/approval_id；
    2B 起每次工具调用记录 tool 事件（tool/入参/出参/耗时/状态/错误），
    由域 emit_artifact 带回编排，console 合入 agent_steps。
    """
    import time as _time

    from langchain_core.messages import ToolMessage
    from langchain_core.runnables import RunnableConfig

    tools_by_name = {t.name: t for t in tools}

    def _run(state: dict, config: RunnableConfig | None = None):
        from shared.request_context import current_request_meta, record_tool_event

        meta = {
            **current_request_meta(),
            **((config or {}).get("metadata") or {}),
        }
        inject = {
            k: meta[k] for k in ("confirm_token", "approval_id")
            if meta.get(k)
        }
        last = (state or {}).get("messages", [])[-1]
        calls = list(getattr(last, "tool_calls", None) or [])
        if inject and calls:
            new_calls = []
            for call in calls:
                args = dict(call.get("args") or {})
                for key, value in inject.items():
                    args.setdefault(key, value)
                new_calls.append({**call, "args": args})
            calls = new_calls
            last = last.model_copy(update={"tool_calls": new_calls})
            state = {**state, "messages": [
                *(state or {}).get("messages", [])[:-1], last]}

        results: list[ToolMessage] = []
        for call in calls:
            name = str(call.get("name") or "")
            tool = tools_by_name.get(name)
            args = dict(call.get("args") or {})
            started = _time.perf_counter()
            status, error = "completed", ""
            content = ""
            if tool is None:
                content = json.dumps(
                    {"error": f"unknown_tool {name}"}, ensure_ascii=False)
                status, error = "failed", f"unknown_tool {name}"
            else:
                try:
                    content = str(tool.invoke(args, config=config))
                except Exception as exc:  # noqa: BLE001
                    content = json.dumps(
                        {"error": str(exc)}, ensure_ascii=False)
                    status, error = "failed", str(exc)
            latency_ms = int((_time.perf_counter() - started) * 1000)
            record_tool_event({
                "domain": domain,
                "tool": name,
                "skill": tool_to_skill.get(name, name),
                "input_summary": json.dumps(args, ensure_ascii=False)[:300],
                "output_summary": content[:400],
                "latency_ms": latency_ms,
                "status": status,
                "error": error,
            })
            audit_from_config(
                config, "tool.call",
                target=tool_to_skill.get(name, name or "unknown"),
                params={"tool": name, "args": args},
                result_summary=content[:300],
                domain=domain,
                latency_ms=latency_ms,
                status=status,
                error=error,
            )
            results.append(ToolMessage(
                content=content, tool_call_id=str(call.get("id") or ""),
                name=name,
            ))
        return {"messages": results}

    return _run
