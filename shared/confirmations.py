"""一次性确认单（写操作两阶段确认）。

1B 起 confirm_token 是服务端随机 UUID，绑定 tenant/actor/customer/action/
target/payload hash，并有一次性和 TTL；不再用可预测的 biz_key。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone

from shared.config import get_settings
from shared.security_db import _lock, get_conn, now


def _remote() -> bool:
    return get_settings().security_mode == "remote"


def _client():  # noqa: ANN202
    from shared.security_client import get_client

    return get_client()


def _payload_hash(payload: dict) -> str:
    raw = json.dumps(payload or {}, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _ensure() -> None:
    conn = get_conn()
    with _lock, conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS confirmations (
                token TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                customer_id TEXT,
                action TEXT NOT NULL,
                target TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                context_id TEXT,
                task_id TEXT,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                consumed_at TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_confirmations_tenant_status "
            "ON confirmations(tenant_id, status)"
        )


def _is_expired(row: dict) -> bool:
    try:
        return datetime.fromisoformat(str(row["expires_at"])) < datetime.now(
            timezone.utc
        )
    except (ValueError, KeyError):
        return True


def create_confirmation(
    *,
    tenant_id: str,
    actor_id: str,
    customer_id: str,
    action: str,
    target: str,
    payload: dict,
    context_id: str = "",
    task_id: str = "",
    ttl_seconds: int = 600,
) -> dict:
    if _remote():
        return _client().confirmation_create({
            "tenant_id": tenant_id, "actor_id": actor_id,
            "customer_id": customer_id, "action": action, "target": target,
            "payload": payload, "context_id": context_id, "task_id": task_id,
            "ttl_seconds": ttl_seconds,
        })
    _ensure()
    token = uuid.uuid4().hex
    created = now()
    expires = (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).isoformat(
        timespec="seconds"
    )
    with _lock, get_conn() as conn:
        conn.execute(
            "INSERT INTO confirmations(token, tenant_id, actor_id, customer_id, "
            "action, target, payload_json, payload_hash, status, context_id, task_id, "
            "created_at, expires_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                token, tenant_id, actor_id, customer_id, action, target,
                json.dumps(payload or {}, ensure_ascii=False),
                _payload_hash(payload), "pending", context_id, task_id,
                created, expires,
            ),
        )
    return {"confirm_token": token, "expires_at": expires}


def get_confirmation(token: str, *, tenant_id: str) -> dict | None:
    if _remote():
        return _client().confirmation_get(token, tenant_id)
    _ensure()
    with _lock:
        row = get_conn().execute(
            "SELECT * FROM confirmations WHERE token=? AND tenant_id=?",
            (token, tenant_id),
        ).fetchone()
    if row is None:
        return None
    d = dict(row)
    if d["status"] == "pending" and _is_expired(d):
        d["status"] = "expired"
    return d


def consume_confirmation(
    token: str,
    *,
    tenant_id: str,
    actor_id: str,
    customer_id: str,
    action: str,
    target: str,
    payload: dict,
) -> dict:
    if _remote():
        return _client().confirmation_consume(token, {
            "tenant_id": tenant_id, "actor_id": actor_id,
            "customer_id": customer_id, "action": action, "target": target,
            "payload": payload,
        })
    _ensure()
    with _lock:
        conn = get_conn()
        if conn.in_transaction:
            conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT * FROM confirmations WHERE token=? AND tenant_id=?",
                (token, tenant_id),
            ).fetchone()
            if row is None:
                conn.rollback()
                return {"ok": False, "error": "not_found"}
            d = dict(row)
            if d["status"] != "pending":
                conn.rollback()
                return {"ok": False, "error": f"status_{d['status']}"}
            if _is_expired(d):
                conn.rollback()
                return {"ok": False, "error": "expired"}
            if (
                d["actor_id"] != actor_id
                or (d.get("customer_id") or "") != (customer_id or "")
                or d["action"] != action
                or d["target"] != target
            ):
                conn.rollback()
                return {"ok": False, "error": "confirmation_scope_mismatch"}
            if d["payload_hash"] != _payload_hash(payload):
                conn.rollback()
                return {"ok": False, "error": "confirmation_payload_mismatch"}
            cur = conn.execute(
                "UPDATE confirmations SET status='consumed', consumed_at=? "
                "WHERE token=? AND status='pending'",
                (now(), token),
            )
            if cur.rowcount != 1:
                conn.rollback()
                return {"ok": False, "error": "already_consumed"}
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return {"ok": True, "confirmation": {"token": token, "status": "consumed"}}
