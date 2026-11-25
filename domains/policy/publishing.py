"""政策发布工作流：draft → submitted → approved → published（四眼原则）。

设计边界：
- 政策正文仍只写 `policy_docs`（权威表），检索内核完全不变；
- 草稿与审批状态放 `policy_drafts`，发布时插入新版本行、关闭旧版开放区间；
- 旧版本只失效不删除；生效期由检索层硬过滤（未来版本=定时生效）；
- 四眼：审批人 ≠ 起草人；发布人 ≠ 起草人；每次状态迁移写审计（由 API 层落）。
"""

from __future__ import annotations

import re
import secrets
from datetime import date, datetime, timedelta, timezone

from domains.policy.chunking import auto_keywords
from shared.db import connect_business_db

CLAUSE_RE = re.compile(r"^CLS-\d+$")
STATUSES = ("draft", "submitted", "approved", "rejected", "published")


class PolicyError(Exception):
    """工作流错误：code 用于 API 错误体，status 用于 HTTP 状态码。"""

    def __init__(self, code: str, detail: str = "", status: int = 400) -> None:
        super().__init__(detail or code)
        self.code = code
        self.detail = detail or code
        self.status = status


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_date(value: str, field: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise PolicyError("bad_date", f"{field} 必须是 YYYY-MM-DD") from exc


def ensure_schema(conn) -> None:  # noqa: ANN001
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS policy_drafts (
            tenant_id TEXT NOT NULL,
            draft_id TEXT NOT NULL,
            clause_id TEXT NOT NULL,
            base_version TEXT,
            title TEXT NOT NULL,
            text TEXT NOT NULL,
            keywords TEXT NOT NULL,
            effective_from TEXT NOT NULL,
            effective_to TEXT,
            status TEXT NOT NULL DEFAULT 'draft',
            author_id TEXT NOT NULL,
            submitted_by TEXT,
            reviewed_by TEXT,
            published_by TEXT,
            review_note TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            published_at TEXT,
            PRIMARY KEY (tenant_id, draft_id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_policy_drafts_clause "
        "ON policy_drafts(tenant_id, clause_id, status)"
    )
    cols = {r[1] for r in conn.execute("PRAGMA table_info(policy_drafts)")}
    if cols and "source_doc_id" not in cols:
        conn.execute("ALTER TABLE policy_drafts ADD COLUMN source_doc_id TEXT")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS policy_documents (
            tenant_id TEXT NOT NULL,
            doc_id TEXT NOT NULL,
            filename TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            uploaded_by TEXT NOT NULL,
            clause_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            PRIMARY KEY (tenant_id, doc_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS policy_index_outbox (
            tenant_id TEXT NOT NULL,
            clause_id TEXT NOT NULL,
            version TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (tenant_id, clause_id, version)
        )
        """
    )


def _normalize(payload: dict) -> dict:
    clause_id = str(payload.get("clause_id") or "").strip().upper()
    if not CLAUSE_RE.match(clause_id):
        raise PolicyError("bad_clause_id", "clause_id 形如 CLS-001")
    title = str(payload.get("title") or "").strip()
    text = str(payload.get("text") or "").strip()
    keywords = str(payload.get("keywords") or "").strip()
    if not title or not text:
        raise PolicyError("missing_params", "title/text 必填")
    if not keywords:
        # 新政策不再要求人工调词：标题 + 正文自动生成，可在草稿阶段人工修改
        keywords = auto_keywords(title, text)
    eff_from = str(payload.get("effective_from") or "").strip()
    eff_to = str(payload.get("effective_to") or "").strip()
    start = _parse_date(eff_from, "effective_from")
    if eff_to:
        end = _parse_date(eff_to, "effective_to")
        if end <= start:
            raise PolicyError("bad_date_range", "effective_to 必须晚于 effective_from")
    return {
        "clause_id": clause_id, "title": title, "text": text,
        "keywords": keywords, "effective_from": start.isoformat(),
        "effective_to": eff_to or None,
    }


def _row(conn, draft_id: str, tenant: str):  # noqa: ANN001
    row = conn.execute(
        "SELECT * FROM policy_drafts WHERE tenant_id=? AND draft_id=?",
        (tenant, draft_id),
    ).fetchone()
    return dict(row) if row else None


def _require(conn, tenant: str, draft_id: str) -> dict:  # noqa: ANN001
    ensure_schema(conn)
    draft = _row(conn, draft_id, tenant)
    if draft is None:
        raise PolicyError("not_found", "草稿不存在", 404)
    return draft


def _transition(conn, draft: dict, *, tenant: str, status: str,  # noqa: ANN001
                field: str, actor: str, extra: dict | None = None) -> dict:
    sets = [f"{field}=?", "status=?", "updated_at=?"]
    args: list = [actor, status, _now()]
    for key, value in (extra or {}).items():
        sets.append(f"{key}=?")
        args.append(value)
    args += [tenant, draft["draft_id"]]
    conn.execute(
        f"UPDATE policy_drafts SET {', '.join(sets)} "
        "WHERE tenant_id=? AND draft_id=?",
        tuple(args),
    )
    return _row(conn, draft["draft_id"], tenant)


def _insert_draft(conn, tenant: str, author_id: str, payload: dict,  # noqa: ANN001
                  source_doc_id: str = "") -> dict:
    data = _normalize(payload)
    current = conn.execute(
        "SELECT version FROM policy_docs WHERE tenant_id=? AND clause_id=? "
        "ORDER BY effective_from DESC LIMIT 1",
        (tenant, data["clause_id"]),
    ).fetchone()
    draft_id = f"PD-{secrets.token_hex(6).upper()}"
    now = _now()
    conn.execute(
        "INSERT INTO policy_drafts(tenant_id, draft_id, clause_id, "
        "base_version, title, text, keywords, effective_from, effective_to, "
        "status, author_id, source_doc_id, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,'draft',?,?,?,?)",
        (tenant, draft_id, data["clause_id"],
         current["version"] if current else None,
         data["title"], data["text"], data["keywords"],
         data["effective_from"], data["effective_to"],
         author_id, source_doc_id or None, now, now),
    )
    return _row(conn, draft_id, tenant) or {}


def create_draft(tenant: str, author_id: str, payload: dict, *,
                 source_doc_id: str = "") -> dict:
    with connect_business_db() as conn:
        ensure_schema(conn)
        return _insert_draft(conn, tenant, author_id, payload, source_doc_id)


def list_drafts(tenant: str, status: str | None = None,
                clause_id: str = "", limit: int = 50) -> list[dict]:
    with connect_business_db() as conn:
        ensure_schema(conn)
        sql = "SELECT * FROM policy_drafts WHERE tenant_id=?"
        args: list = [tenant]
        if status:
            sql += " AND status=?"
            args.append(status)
        if clause_id:
            sql += " AND clause_id=?"
            args.append(clause_id.strip().upper())
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(max(1, min(limit, 200)))
        return [dict(r) for r in conn.execute(sql, tuple(args)).fetchall()]


def get_draft(tenant: str, draft_id: str) -> dict | None:
    with connect_business_db() as conn:
        ensure_schema(conn)
        return _row(conn, draft_id, tenant)


def submit_draft(tenant: str, draft_id: str, actor_id: str) -> dict:
    with connect_business_db() as conn:
        draft = _require(conn, tenant, draft_id)
        if draft["status"] != "draft":
            raise PolicyError("bad_status", f"当前状态 {draft['status']} 不可提交", 409)
        if draft["author_id"] != actor_id:
            raise PolicyError("author_only", "仅起草人可提交", 403)
        return _transition(conn, draft, tenant=tenant, status="submitted",
                           field="submitted_by", actor=actor_id)


def approve_draft(tenant: str, draft_id: str, reviewer_id: str) -> dict:
    with connect_business_db() as conn:
        draft = _require(conn, tenant, draft_id)
        if draft["status"] != "submitted":
            raise PolicyError("bad_status", f"当前状态 {draft['status']} 不可审批", 409)
        if reviewer_id == draft["author_id"]:
            raise PolicyError("four_eyes", "审批人不能是起草人（四眼原则）", 403)
        return _transition(conn, draft, tenant=tenant, status="approved",
                           field="reviewed_by", actor=reviewer_id)


def reject_draft(tenant: str, draft_id: str, reviewer_id: str,
                 note: str = "") -> dict:
    with connect_business_db() as conn:
        draft = _require(conn, tenant, draft_id)
        if draft["status"] != "submitted":
            raise PolicyError("bad_status", f"当前状态 {draft['status']} 不可驳回", 409)
        if reviewer_id == draft["author_id"]:
            raise PolicyError("four_eyes", "审批人不能是起草人（四眼原则）", 403)
        return _transition(conn, draft, tenant=tenant, status="rejected",
                           field="reviewed_by", actor=reviewer_id,
                           extra={"review_note": note or "未通过"})


def _next_version(conn, tenant: str, clause_id: str) -> str:  # noqa: ANN001
    row = conn.execute(
        "SELECT MAX(CAST(REPLACE(version, 'v', '') AS INTEGER)) AS v "
        "FROM policy_docs WHERE tenant_id=? AND clause_id=?",
        (tenant, clause_id),
    ).fetchone()
    return f"v{(row['v'] or 0) + 1}"


def publish_draft(tenant: str, draft_id: str, publisher_id: str) -> dict:
    """审批通过后发布：插入 policy_docs 新版本 + 关闭上一版开放区间。"""
    with connect_business_db() as conn:
        draft = _require(conn, tenant, draft_id)
        if draft["status"] != "approved":
            raise PolicyError("bad_status", f"当前状态 {draft['status']} 不可发布", 409)
        if publisher_id == draft["author_id"]:
            raise PolicyError("four_eyes", "发布人不能是起草人（四眼原则）", 403)

        clause_id = draft["clause_id"]
        start = _parse_date(draft["effective_from"], "effective_from")
        end = _parse_date(draft["effective_to"], "effective_to") \
            if draft["effective_to"] else None
        if end and end <= start:
            raise PolicyError("bad_date_range", "effective_to 必须晚于 effective_from")
        if start < date.today():
            raise PolicyError(
                "backdated",
                "生效期不能早于今天（追溯生效需单独审批通道）", 409)

        versions = conn.execute(
            "SELECT version, effective_from, effective_to FROM policy_docs "
            "WHERE tenant_id=? AND clause_id=? ORDER BY effective_from",
            (tenant, clause_id),
        ).fetchall()
        if versions:
            latest = max(versions, key=lambda r: r["effective_from"])
            latest_start = _parse_date(latest["effective_from"], "effective_from")
            if start <= latest_start:
                raise PolicyError(
                    "version_order",
                    f"新版本生效期必须晚于现行版本 {latest['version']}"
                    f"（{latest['effective_from']}）", 409)
            if latest["effective_to"]:
                latest_end = _parse_date(latest["effective_to"], "effective_to")
                if start <= latest_end:
                    raise PolicyError(
                        "overlap", f"与 {latest['version']} 生效区间重叠", 409)
            else:
                # 关闭上一版开放区间；旧版本只失效不删除
                conn.execute(
                    "UPDATE policy_docs SET effective_to=? "
                    "WHERE tenant_id=? AND clause_id=? AND version=?",
                    ((start - timedelta(days=1)).isoformat(),
                     tenant, clause_id, latest["version"]),
                )

        version = _next_version(conn, tenant, clause_id)
        conn.execute(
            "INSERT INTO policy_docs(tenant_id, clause_id, version, title, text, "
            "effective_from, effective_to, keywords) VALUES (?,?,?,?,?,?,?,?)",
            (tenant, clause_id, version, draft["title"], draft["text"],
             draft["effective_from"], draft["effective_to"], draft["keywords"]),
        )
        # 同事务写索引 outbox：跨库（SQLite→pgvector）不做分布式事务，
        # 由 scripts/index_policies.py --drain 最终一致地补索引。
        now = _now()
        conn.execute(
            "INSERT INTO policy_index_outbox(tenant_id, clause_id, version, "
            "status, attempts, created_at, updated_at) "
            "VALUES (?,?,?,'pending',0,?,?) "
            "ON CONFLICT(tenant_id, clause_id, version) DO UPDATE SET "
            "status='pending', attempts=0, last_error=NULL, "
            "updated_at=excluded.updated_at",
            (tenant, clause_id, version, now, now),
        )
        updated = _transition(
            conn, draft, tenant=tenant, status="published",
            field="published_by", actor=publisher_id,
            extra={"published_at": _now()},
        )
        updated["version"] = version
        return updated


def _current_clause(conn, tenant: str, clause_id: str) -> dict | None:  # noqa: ANN001
    today = date.today().isoformat()
    row = conn.execute(
        "SELECT * FROM policy_docs WHERE tenant_id=? AND clause_id=? "
        "AND effective_from <= ? "
        "AND (effective_to IS NULL OR effective_to='' OR effective_to > ?) "
        "ORDER BY effective_from DESC LIMIT 1",
        (tenant, clause_id, today, today),
    ).fetchone()
    return dict(row) if row else None


def diff_draft(tenant: str, draft_id: str) -> dict:
    """字段级 diff：草稿 vs 当前生效版本（没有现行版本时 old 全为 null）。"""
    with connect_business_db() as conn:
        draft = _require(conn, tenant, draft_id)
        current = _current_clause(conn, tenant, draft["clause_id"])
    fields = ("title", "text", "keywords", "effective_from", "effective_to")
    old = current or {}
    changes = {
        f: {"old": old.get(f), "new": draft.get(f)}
        for f in fields
        if (old.get(f) or "") != (draft.get(f) or "")
    }
    return {
        "draft_id": draft_id,
        "clause_id": draft["clause_id"],
        "base_version": old.get("version"),
        "status": draft["status"],
        "changed": sorted(changes),
        "changes": changes,
    }


# ---------------------------------------------------------------------------
# 知识条目（knowledge_entries）写入：工具（knowledge.upsert）与内部 API 共用
# ---------------------------------------------------------------------------

KNOWLEDGE_STATUSES = ("draft", "submitted", "published", "archived")


def upsert_knowledge_entry(
    tenant_id: str, actor_id: str, *, title: str, content: str, status: str,
    entry_id: str = "", scope: str = "general", effective_from: str = "",
    effective_to: str = "", keywords: str = "", conflicts_with: str = "",
) -> dict:
    """新增/版本化更新知识条目（单份实现，PRD 3.2 双形态共用）。

    返回 {"entry_id", "version", "status", "title"}；
    参数不合法抛 PolicyError（code → API 错误体 / 工具 JSON error）。
    """
    if not title.strip() or not content.strip():
        raise PolicyError("missing_params", "title/content 必填")
    if status not in KNOWLEDGE_STATUSES:
        raise PolicyError("bad_status",
                          f"status 必须是 {'/'.join(KNOWLEDGE_STATUSES)}")
    with connect_business_db() as conn:
        if entry_id.strip():
            eid = entry_id.strip().upper()
            row = conn.execute(
                "SELECT MAX(CAST(REPLACE(version, 'v', '') AS INTEGER)) AS v "
                "FROM knowledge_entries WHERE tenant_id=? AND entry_id=?",
                (tenant_id, eid),
            ).fetchone()
            version = f"v{(row['v'] or 0) + 1}"
        else:
            eid = f"KB-{secrets.token_hex(3).upper()}"
            version = "v1"
        conn.execute(
            "INSERT INTO knowledge_entries(tenant_id, entry_id, version, title, "
            "content, scope, status, effective_from, effective_to, keywords, "
            "conflicts_with, updated_by, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))",
            (tenant_id, eid, version, title.strip(), content.strip(), scope,
             status, effective_from or None, effective_to or None, keywords,
             conflicts_with or None, actor_id),
        )
    return {"entry_id": eid, "version": version, "status": status,
            "title": title.strip()}
