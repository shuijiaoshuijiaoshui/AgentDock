"""政策文档导入：解析 Markdown/TXT → 校验 → 同一事务建文档记录与多条草稿。

导入只产生 draft（仍需四眼审批发布）；不直接改权威表，也不触发索引。
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import date, datetime, timezone
from pathlib import PurePath

from domains.policy.chunking import ChunkingError, parse_document
from domains.policy.publishing import _insert_draft, ensure_schema
from shared.config import get_settings
from shared.db import connect_business_db

MAX_DOC_BYTES = 1_000_000
ALLOWED_SUFFIXES = (".md", ".txt")


class IngestionError(Exception):
    def __init__(self, code: str, detail: str, line: int = 0,
                 status: int = 400) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.line = line
        self.status = status


def import_document(tenant_id: str, author_id: str, filename: str,
                    raw: bytes, *, effective_from: str = "") -> dict:
    name = PurePath(filename or "policy.md").name
    if PurePath(name).suffix.lower() not in ALLOWED_SUFFIXES:
        raise IngestionError("bad_file_type", "仅支持 .md / .txt")
    if not raw:
        raise IngestionError("empty_file", "文件为空")
    if len(raw) > MAX_DOC_BYTES:
        raise IngestionError("file_too_large", "文件超过 1MB")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise IngestionError("bad_encoding", "仅支持 UTF-8 文本") from exc

    try:
        clauses = parse_document(
            text, max_chars=int(get_settings().policy_chunk_max_chars))
    except ChunkingError as exc:
        raise IngestionError(exc.code, exc.detail, exc.line) from exc

    eff = (effective_from or date.today().isoformat()).strip()
    doc_id = f"DOC-{secrets.token_hex(6).upper()}"
    digest = hashlib.sha256(raw).hexdigest()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    with connect_business_db() as conn:
        ensure_schema(conn)
        conn.execute(
            "INSERT INTO policy_documents(tenant_id, doc_id, filename, sha256, "
            "uploaded_by, clause_count, created_at) VALUES (?,?,?,?,?,?,?)",
            (tenant_id, doc_id, name, digest, author_id, len(clauses), now),
        )
        drafts = []
        for clause in clauses:
            drafts.append(_insert_draft(
                conn, tenant_id, author_id,
                {
                    "clause_id": clause["clause_id"],
                    "title": clause["title"],
                    "text": clause["text"],
                    "keywords": clause["keywords"],
                    "effective_from": eff,
                },
                source_doc_id=doc_id,
            ))
    return {
        "doc_id": doc_id,
        "filename": name,
        "sha256": digest,
        "clause_count": len(clauses),
        "drafts": drafts,
        "warnings": [],
    }
