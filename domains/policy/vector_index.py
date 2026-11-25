"""pgvector 索引：policy_chunks 的建表 / 写入 / 检索 / 状态。

唯一索引存储；权威数据仍在 SQLite policy_docs。检索返回 clause_id+version，
调用方回 SQLite 取权威行并再次过滤生效期/租户（双保险）。
"""

from __future__ import annotations

import logging
from datetime import date

import psycopg

from shared.config import get_settings

logger = logging.getLogger(__name__)


class VectorUnavailable(RuntimeError):
    pass


def _dsn() -> str:
    s = get_settings()
    return s.policy_vector_dsn or s.postgres_dsn


def _vec_literal(vec: list[float]) -> str:
    return "[" + ",".join(f"{float(x):.7f}" for x in vec) + "]"


def _connect(timeout: int = 3):
    try:
        return psycopg.connect(_dsn(), autocommit=True, connect_timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        raise VectorUnavailable(f"pgvector 不可用：{exc}") from exc


def ensure_schema(conn) -> None:  # noqa: ANN001
    s = get_settings()
    dim = int(s.embedding_dim)
    try:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS policy_chunks (
                tenant_id TEXT NOT NULL,
                chunk_id TEXT NOT NULL,
                clause_id TEXT NOT NULL,
                version TEXT NOT NULL,
                chunk_index INTEGER NOT NULL,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                keywords TEXT NOT NULL DEFAULT '',
                effective_from DATE NOT NULL,
                effective_to DATE,
                content_hash TEXT NOT NULL,
                embedding vector({dim}) NOT NULL,
                model_name TEXT NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (tenant_id, chunk_id)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_policy_chunks_clause "
            "ON policy_chunks(tenant_id, clause_id, version)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_policy_chunks_vec "
            "ON policy_chunks USING hnsw (embedding vector_cosine_ops)"
        )
    except Exception as exc:  # noqa: BLE001
        raise VectorUnavailable(f"初始化 pgvector 失败：{exc}") from exc


def ping() -> bool:
    try:
        with _connect() as conn:
            conn.execute("SELECT 1")
        return True
    except VectorUnavailable:
        return False


def clear_tenant(tenant_id: str) -> int:
    with _connect() as conn:
        ensure_schema(conn)
        cur = conn.execute(
            "DELETE FROM policy_chunks WHERE tenant_id=%s", (tenant_id,))
        return cur.rowcount or 0


def upsert_clause(tenant_id: str, clause_id: str, version: str,
                  chunks: list[dict], embeddings: list[list[float]],
                  *, model_name: str, title: str, keywords: str,
                  effective_from: str, effective_to: str | None) -> int:
    """覆盖写一个条款版本的所有 chunk（先删后插，幂等）。"""
    if len(chunks) != len(embeddings):
        raise ValueError("chunks 与 embeddings 数量不一致")
    with _connect() as conn:
        ensure_schema(conn)
        conn.execute(
            "DELETE FROM policy_chunks WHERE tenant_id=%s AND clause_id=%s "
            "AND version=%s",
            (tenant_id, clause_id, version),
        )
        for chunk, vec in zip(chunks, embeddings):
            chunk_id = f"{clause_id}:{version}:{chunk['chunk_index']}"
            conn.execute(
                "INSERT INTO policy_chunks(tenant_id, chunk_id, clause_id, "
                "version, chunk_index, title, content, keywords, "
                "effective_from, effective_to, content_hash, embedding, "
                "model_name) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
                "%s::vector,%s)",
                (tenant_id, chunk_id, clause_id, version,
                 int(chunk["chunk_index"]), title, chunk["text"], keywords,
                 effective_from, effective_to or None, chunk["content_hash"],
                 _vec_literal(vec), model_name),
            )
    return len(chunks)


def search(query_vec: list[float], tenant_id: str, *,
           limit: int = 24) -> list[dict]:
    """向量召回：租户 + 生效期 + 模型过滤；HNSW 过滤有损，调用方多取。"""
    s = get_settings()
    today = date.today().isoformat()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT clause_id, version, chunk_index, title, content, "
            "1 - (embedding <=> %s::vector) AS similarity "
            "FROM policy_chunks "
            "WHERE tenant_id=%s AND model_name=%s "
            "AND effective_from <= %s "
            "AND (effective_to IS NULL OR effective_to > %s) "
            "ORDER BY embedding <=> %s::vector LIMIT %s",
            (_vec_literal(query_vec), tenant_id, s.embedding_model, today,
             today, _vec_literal(query_vec), int(limit)),
        ).fetchall()
    return [
        {"clause_id": r[0], "version": r[1], "chunk_index": r[2],
         "title": r[3], "content": r[4], "similarity": float(r[5])}
        for r in rows
    ]


def status(tenant_id: str = "") -> dict:
    with _connect() as conn:
        ensure_schema(conn)
        where = "WHERE tenant_id=%s" if tenant_id else ""
        args = (tenant_id,) if tenant_id else ()
        row = conn.execute(
            f"SELECT count(*), count(DISTINCT clause_id), "
            f"count(DISTINCT version) FROM policy_chunks {where}", args
        ).fetchone()
        model = get_settings().embedding_model
        return {"chunks": int(row[0]), "clauses": int(row[1]),
                "versions": int(row[2]), "model": model,
                "dim": int(get_settings().embedding_dim)}
