"""审批单（高价值动作的人机闸门 + 双人复核）。

1B 起：
- approval id 为不可枚举的 UUID；
- pending/approved 都校验 expires_at；
- consume 绑定 requested_by + action + target + amount，一单一次；
- 只有申请人或显式授权角色（finance/supervisor/admin）可消费；
- remote 模式经 securityd 单写者，local 模式仅单进程测试。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

from shared.audit import audit_event
from shared.config import get_settings
from shared.security_db import _lock, get_conn, now

_CONSUMER_ROLES = {"finance", "supervisor", "admin"}


def _remote() -> bool:
    return get_settings().security_mode == "remote"


def _client():  # noqa: ANN202
    from shared.security_client import get_client

    return get_client()


def _begin(conn) -> None:  # noqa: ANN001
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")


def _ensure() -> None:
    conn = get_conn()
    with _lock, conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(approvals)")}
        if cols and not {"conversation_id", "resume", "execution_status"} <= cols:
            _migrate_legacy(conn)
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS approvals (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                action TEXT NOT NULL,
                target TEXT NOT NULL,
                amount REAL,
                requested_by TEXT NOT NULL,
                requested_role TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                required_approvals INTEGER NOT NULL DEFAULT 1,
                approvals TEXT NOT NULL DEFAULT '[]',
                approver_roles TEXT NOT NULL DEFAULT '[]',
                dual_control INTEGER NOT NULL DEFAULT 0,
                reason TEXT,
                context_id TEXT,
                task_id TEXT,
                conversation_id TEXT,
                resume TEXT,
                execution_status TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                expires_at TEXT,
                executed_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_approvals_tenant_status
                ON approvals(tenant_id, status, created_at);
            CREATE INDEX IF NOT EXISTS idx_approvals_target
                ON approvals(tenant_id, action, target, status);
            """
        )


def _migrate_legacy(conn) -> None:  # noqa: ANN001
    old = conn.execute("SELECT * FROM approvals").fetchall()
    conn.execute("ALTER TABLE approvals RENAME TO approvals_legacy")
    conn.execute(
        """
        CREATE TABLE approvals (
            id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, action TEXT NOT NULL,
            target TEXT NOT NULL, amount REAL, requested_by TEXT NOT NULL,
            requested_role TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
            required_approvals INTEGER NOT NULL DEFAULT 1,
            approvals TEXT NOT NULL DEFAULT '[]',
            approver_roles TEXT NOT NULL DEFAULT '[]',
            dual_control INTEGER NOT NULL DEFAULT 0, reason TEXT,
            context_id TEXT, task_id TEXT, conversation_id TEXT, resume TEXT,
            execution_status TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            expires_at TEXT, executed_at TEXT
        )
        """
    )
    for row in old:
        d = dict(row)
        conn.execute(
            "INSERT INTO approvals(id, tenant_id, action, target, amount, requested_by, "
            "requested_role, status, required_approvals, approvals, approver_roles, "
            "dual_control, reason, context_id, task_id, conversation_id, resume, "
            "execution_status, created_at, updated_at, expires_at, executed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"APR-{uuid.uuid4().hex}", d.get("tenant_id"), d.get("action"),
                d.get("target"), d.get("amount"), d.get("requested_by"),
                d.get("requested_role"), d.get("status") or "pending",
                int(d.get("required_approvals") or 1), d.get("approvals") or "[]",
                d.get("approver_roles") or "[]", int(d.get("dual_control") or 0),
                d.get("reason"), d.get("context_id"), d.get("task_id"), "", "{}",
                None, d.get("created_at") or now(), d.get("updated_at") or now(),
                d.get("expires_at"), d.get("executed_at"),
            ),
        )
    conn.execute("DROP TABLE approvals_legacy")


def _row_to_dict(row) -> dict:  # noqa: ANN001
    d = dict(row)
    for key in ("approvals", "approver_roles"):
        try:
            d[key] = json.loads(d.get(key) or "[]")
        except json.JSONDecodeError:
            d[key] = []
    try:
        d["resume"] = json.loads(d.get("resume") or "{}")
    except json.JSONDecodeError:
        d["resume"] = {}
    d["dual_control"] = bool(d.get("dual_control"))
    return d


def _is_expired(d: dict) -> bool:
    raw = d.get("expires_at")
    if not raw:
        return False
    try:
        return datetime.fromisoformat(raw) < datetime.now(timezone.utc)
    except ValueError:
        return False


def _expire_if_needed(d: dict) -> dict:
    if d.get("status") in ("pending", "approved") and _is_expired(d):
        d["status"] = "expired"
    return d


def create_approval(
    *,
    tenant_id: str,
    action: str,
    target: str,
    amount: float | None,
    requested_by: str,
    requested_role: str,
    required_approvals: int,
    approver_roles: tuple[str, ...],
    dual_control: bool,
    reason: str = "",
    context_id: str = "",
    task_id: str = "",
    conversation_id: str = "",
    resume: dict | None = None,
    ttl_minutes: int | None = None,
) -> dict:
    if _remote():
        return _client().approval_create({
            "tenant_id": tenant_id, "action": action, "target": target,
            "amount": amount, "requested_by": requested_by,
            "requested_role": requested_role,
            "required_approvals": required_approvals,
            "approver_roles": list(approver_roles),
            "dual_control": dual_control, "reason": reason,
            "context_id": context_id, "task_id": task_id,
            "conversation_id": conversation_id, "resume": resume or {},
            "ttl_minutes": ttl_minutes,
        })
    _ensure()
    s = get_settings()
    ttl = ttl_minutes if ttl_minutes is not None else s.approval_ttl_minutes
    created = now()
    expires = (datetime.now(timezone.utc) + timedelta(minutes=ttl)).isoformat(
        timespec="seconds"
    )
    approval_id = f"APR-{uuid.uuid4().hex}"
    with _lock, get_conn() as conn:
        conn.execute(
            "INSERT INTO approvals(id, tenant_id, action, target, amount, requested_by, "
            "requested_role, status, required_approvals, approvals, approver_roles, "
            "dual_control, reason, context_id, task_id, conversation_id, resume, "
            "execution_status, created_at, updated_at, expires_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                approval_id, tenant_id, action, target, amount, requested_by,
                requested_role, "pending", required_approvals, "[]",
                json.dumps(list(approver_roles)), 1 if dual_control else 0,
                reason, context_id, task_id, conversation_id,
                json.dumps(resume or {}, ensure_ascii=False), "pending",
                created, created, expires,
            ),
        )
    audit_event(
        tenant_id=tenant_id, actor_id=requested_by, actor_role=requested_role,
        action="approval.request", target=f"{action}:{target}",
        params={"approval_id": approval_id, "amount": amount,
                "required_approvals": required_approvals,
                "dual_control": dual_control},
        result_summary=reason, context_id=context_id, task_id=task_id,
        strict=True,
    )
    return get_approval(tenant_id, approval_id) or {}


def get_approval(tenant_id: str, approval_id: str) -> dict | None:
    if _remote():
        return _client().approval_get(tenant_id, approval_id)
    _ensure()
    with _lock:
        row = get_conn().execute(
            "SELECT * FROM approvals WHERE id=? AND tenant_id=?",
            (approval_id, tenant_id),
        ).fetchone()
    return _expire_if_needed(_row_to_dict(row)) if row else None


def list_approvals(
    tenant_id: str, status: str | None = None, limit: int = 50
) -> list[dict]:
    if _remote():
        return _client().approval_list(tenant_id, status, limit)
    _ensure()
    with _lock:
        if status:
            rows = get_conn().execute(
                "SELECT * FROM approvals WHERE tenant_id=? AND status=? "
                "ORDER BY created_at DESC LIMIT ?",
                (tenant_id, status, limit),
            ).fetchall()
        else:
            rows = get_conn().execute(
                "SELECT * FROM approvals WHERE tenant_id=? "
                "ORDER BY created_at DESC LIMIT ?",
                (tenant_id, limit),
            ).fetchall()
    return [_expire_if_needed(_row_to_dict(row)) for row in rows]


def find_approved(
    tenant_id: str, *, action: str, target: str, amount: float | None,
    requested_by: str,
) -> dict | None:
    """找同一申请人、同一动作/对象/金额的未过期已批准单。"""
    if _remote():
        return _client().approval_find(
            tenant_id=tenant_id, action=action, target=target,
            amount=amount, requested_by=requested_by,
        )
    for d in list_approvals(tenant_id, status="approved", limit=100):
        if d.get("status") != "approved":
            continue
        if d["action"] != action or d["target"] != target:
            continue
        if d.get("requested_by") != requested_by:
            continue
        if amount is None or d.get("amount") is None:
            continue
        if abs(float(d["amount"]) - float(amount)) > 0.01:
            continue
        return d
    return None


def find_pending(tenant_id: str, *, action: str, target: str) -> dict | None:
    """找同动作/同对象的未过期待审批单（防止重复申请堆叠）。

    已过期的单会在 list_approvals 里被翻转为 expired，不会阻塞新申请。
    """
    for d in list_approvals(tenant_id, status="pending", limit=100):
        if d.get("action") == action and d.get("target") == target:
            return d
    return None


def supersede_siblings(
    tenant_id: str, *, action: str, target: str,
    except_id: str | None = None, reason: str = "",
    actor_id: str = "system", role: str = "service",
) -> int:
    """关闭同动作/同对象的其余未终态审批单（pending/approved → superseded）。

    金额操作收口的联动机制：同目标只允许一张生效审批单；被关闭的单保留
    原记录可追责。返回关闭数量。失败由调用方决定是否吞掉（清理失败不应
    影响已完成的业务结果）。
    """
    if _remote():
        return _client().approval_supersede(
            tenant_id, action=action, target=target,
            except_id=except_id, reason=reason, actor_id=actor_id, role=role,
        )
    _ensure()
    closed = 0
    with _lock:
        conn = get_conn()
        _begin(conn)
        try:
            rows = conn.execute(
                "SELECT id FROM approvals WHERE tenant_id=? AND action=? "
                "AND target=? AND id!=? AND status IN ('pending','approved')",
                (tenant_id, action, target, except_id or ""),
            ).fetchall()
            for r in rows:
                cur = conn.execute(
                    "UPDATE approvals SET status='superseded', updated_at=? "
                    "WHERE id=? AND tenant_id=? AND status IN ('pending','approved')",
                    (now(), r["id"], tenant_id),
                )
                closed += cur.rowcount
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    if closed:
        audit_event(
            tenant_id=tenant_id, actor_id=actor_id, actor_role=role,
            action="approval.superseded", target=f"{action}:{target}",
            params={"closed": closed, "reason": reason},
            result_summary=f"联动关闭 {closed} 张同目标审批单",
            strict=True,
        )
    return closed


def _approval_roles(d: dict) -> set[str]:
    return {str(a.get("role")) for a in d.get("approvals", []) if a.get("role")}


def approve(
    tenant_id: str, approval_id: str, *, actor_id: str, role: str
) -> dict:
    """批准一票；返回最新审批单。职责分离在这里强制。"""
    if _remote():
        return _client().approval_approve(
            tenant_id, approval_id, actor_id=actor_id, role=role,
        )
    _ensure()
    with _lock:
        conn = get_conn()
        _begin(conn)
        try:
            row = conn.execute(
                "SELECT * FROM approvals WHERE id=? AND tenant_id=?",
                (approval_id, tenant_id),
            ).fetchone()
            if row is None:
                conn.rollback()
                return {"error": "not_found"}
            d = _row_to_dict(row)
            if d["status"] != "pending":
                conn.rollback()
                return {"error": f"status_{d['status']}"}
            if _is_expired(d):
                conn.rollback()
                return {"error": "expired"}
            if actor_id == d["requested_by"]:
                conn.rollback()
                return {"error": "self_approval_forbidden"}
            if role not in set(d.get("approver_roles") or []):
                conn.rollback()
                return {"error": "role_not_allowed",
                        "allowed": d.get("approver_roles")}
            existing = d.get("approvals") or []
            if any(a.get("actor_id") == actor_id for a in existing):
                conn.rollback()
                return {"error": "already_approved_by_actor"}
            existing.append({"actor_id": actor_id, "role": role, "at": now()})
            roles = {a.get("role") for a in existing}
            enough = len(existing) >= int(d["required_approvals"])
            if d.get("dual_control"):
                enough = (
                    enough and "finance" in roles
                    and ("supervisor" in roles or "admin" in roles)
                )
            status = "approved" if enough else "pending"
            cur = conn.execute(
                "UPDATE approvals SET approvals=?, status=?, updated_at=? "
                "WHERE id=? AND tenant_id=? AND status='pending'",
                (json.dumps(existing, ensure_ascii=False), status, now(),
                 approval_id, tenant_id),
            )
            if cur.rowcount != 1:
                conn.rollback()
                return {"error": "concurrent_update"}
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    audit_event(
        tenant_id=tenant_id, actor_id=actor_id, actor_role=role,
        action="approval.approve", target=f"{d['action']}:{d['target']}",
        params={"approval_id": approval_id, "status": status,
                "approvers": existing},
        result_summary=d.get("reason", ""), context_id=d.get("context_id", ""),
        task_id=d.get("task_id", ""), strict=True,
    )
    return get_approval(tenant_id, approval_id) or {}


def reject(
    tenant_id: str, approval_id: str, *, actor_id: str, role: str, reason: str = ""
) -> dict:
    if _remote():
        return _client().approval_reject(
            tenant_id, approval_id, actor_id=actor_id, role=role, reason=reason,
        )
    _ensure()
    with _lock:
        conn = get_conn()
        _begin(conn)
        try:
            row = conn.execute(
                "SELECT * FROM approvals WHERE id=? AND tenant_id=?",
                (approval_id, tenant_id),
            ).fetchone()
            if row is None:
                conn.rollback()
                return {"error": "not_found"}
            d = _row_to_dict(row)
            if d["status"] != "pending":
                conn.rollback()
                return {"error": f"status_{d['status']}"}
            if _is_expired(d):
                conn.rollback()
                return {"error": "expired"}
            if role not in set(d.get("approver_roles") or []):
                conn.rollback()
                return {"error": "role_not_allowed"}
            cur = conn.execute(
                "UPDATE approvals SET status='rejected', reason=?, updated_at=? "
                "WHERE id=? AND tenant_id=? AND status='pending'",
                (reason or d.get("reason", ""), now(), approval_id, tenant_id),
            )
            if cur.rowcount != 1:
                conn.rollback()
                return {"error": "concurrent_update"}
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    audit_event(
        tenant_id=tenant_id, actor_id=actor_id, actor_role=role,
        action="approval.reject", target=f"{d['action']}:{d['target']}",
        params={"approval_id": approval_id}, result_summary=reason,
        context_id=d.get("context_id", ""), task_id=d.get("task_id", ""),
        strict=True,
    )
    return get_approval(tenant_id, approval_id) or {}


def consume(
    tenant_id: str,
    approval_id: str,
    *,
    action: str,
    target: str,
    amount: float | None,
    actor_id: str,
    role: str,
) -> dict:
    """执行前消费审批单：动作/对象/金额/申请人/角色/有效期/一次性全校验。"""
    if _remote():
        return _client().approval_consume(
            tenant_id, approval_id, action=action, target=target,
            amount=amount, actor_id=actor_id, role=role,
        )
    _ensure()
    with _lock:
        conn = get_conn()
        _begin(conn)
        try:
            row = conn.execute(
                "SELECT * FROM approvals WHERE id=? AND tenant_id=?",
                (approval_id, tenant_id),
            ).fetchone()
            if row is None:
                conn.rollback()
                return {"ok": False, "error": "not_found"}
            d = _row_to_dict(row)
            if d["status"] != "approved":
                conn.rollback()
                return {"ok": False, "error": f"status_{d['status']}"}
            if _is_expired(d):
                conn.rollback()
                return {"ok": False, "error": "expired"}
            if d["action"] != action or d["target"] != target:
                conn.rollback()
                return {"ok": False, "error": "approval_scope_mismatch"}
            if amount is None or d.get("amount") is None:
                conn.rollback()
                return {"ok": False, "error": "approval_amount_mismatch"}
            if abs(float(d["amount"]) - float(amount)) > 0.01:
                conn.rollback()
                return {"ok": False, "error": "approval_amount_mismatch"}
            if actor_id != d["requested_by"] and role not in _CONSUMER_ROLES:
                conn.rollback()
                return {"ok": False, "error": "actor_not_allowed"}
            cur = conn.execute(
                "UPDATE approvals SET status='executed', executed_at=?, "
                "execution_status='executed', updated_at=? "
                "WHERE id=? AND tenant_id=? AND status='approved'",
                (now(), now(), approval_id, tenant_id),
            )
            if cur.rowcount != 1:
                conn.rollback()
                return {"ok": False, "error": "already_consumed"}
            # 同目标审批单联动 supersede：同对象只允许一张生效审批单。
            # 与上面的消费同事务，跨库业务侧无需再关心清理。
            sib_rows = conn.execute(
                "SELECT id, status FROM approvals WHERE tenant_id=? AND action=? "
                "AND target=? AND id!=? AND status IN ('pending','approved')",
                (tenant_id, action, target, approval_id),
            ).fetchall()
            superseded_ids = []
            for r in sib_rows:
                cur2 = conn.execute(
                    "UPDATE approvals SET status='superseded', updated_at=? "
                    "WHERE id=? AND tenant_id=? AND status IN ('pending','approved')",
                    (now(), r["id"], tenant_id),
                )
                if cur2.rowcount == 1:
                    superseded_ids.append(r["id"])
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    audit_event(
        tenant_id=tenant_id, actor_id=actor_id, actor_role=role,
        action="approval.execute", target=f"{action}:{target}",
        params={"approval_id": approval_id, "amount": amount},
        result_summary="审批单已消费并执行", context_id=d.get("context_id", ""),
        task_id=d.get("task_id", ""), strict=True,
    )
    if superseded_ids:
        audit_event(
            tenant_id=tenant_id, actor_id=actor_id, actor_role=role,
            action="approval.superseded", target=f"{action}:{target}",
            params={"closed": superseded_ids,
                    "reason": "同目标审批已执行，联动关闭其余审批单"},
            result_summary=f"联动关闭 {len(superseded_ids)} 张",
            context_id=d.get("context_id", ""), task_id=d.get("task_id", ""),
            strict=True,
        )
    return {"ok": True, "approval": get_approval(tenant_id, approval_id)}
