"""政策检索内核：policy_docs 的生效版过滤与关键词检索。

这份实现被两处共用（PRD 3.2：权威文本、版本、生效期判定只实现一份）：
- 域内 @tool（policy.answer / policy.clause）
- /internal/clauses 只读 API（各域 PolicyClient 的后端）

生效期硬过滤：effective_from ≤ 今天 < effective_to（to 为空 = 长期生效）。
过期版本绝不返回——R11：过期比编造更难发现。
"""

import logging
import re
import sqlite3
from datetime import date

from shared.config import get_settings
from shared.db import business_db_path

logger = logging.getLogger(__name__)


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(business_db_path())
    conn.row_factory = sqlite3.Row
    return conn


def _effective_where() -> str:
    today = date.today().isoformat()
    return (
        f"effective_from <= '{today}' "
        f"AND (effective_to IS NULL OR effective_to = '' OR effective_to > '{today}')"
    )


def _row_to_clause(r: sqlite3.Row) -> dict:
    return {
        "clause_id": r["clause_id"],
        "version": r["version"],
        "title": r["title"],
        "text": r["text"],
        "effective_range": f'{r["effective_from"]} ~ {r["effective_to"] or "长期"}',
    }


_SPLIT_RE = re.compile(r"[？?；;。，,\s]+")


def _terms(query: str) -> list[str]:
    """按标点/空白切子句；单字噪声不参与检索。"""
    return [t for t in _SPLIT_RE.split(query or "") if len(t) >= 2]


def _grams(term: str) -> list[str]:
    return [term[i:i + 2] for i in range(len(term) - 1)] if len(term) >= 2 else []


def _evidence(term: str, curated: str, body: str) -> tuple[bool, int]:
    """强证据判定：标题/关键词命中才算候选，正文只加分或兜底长词。

    背景：长句按 2-gram 兜底时，“购买/商家”这类只出现在正文里的二字片段
    也会得分，导致“购买政策”误召回保修/运费条款。这里要求先命中人工维护
    的 title/keywords（强证据），正文命中仅用于排序；只有长度 ≥3 的完整词
    在正文命中时才兜底（如“原路退回”）。
    """
    if term in curated:
        return True, len(term) * 2
    grams = _grams(term)
    hits = [g for g in grams if g in curated]
    if hits:
        body_hits = sum(1 for g in grams if g in body)
        return True, len(hits) * 2 + body_hits
    if len(term) >= 3 and term in body:
        return True, len(term)
    return False, 0


def _search_keyword(query: str, limit: int = 3, *,
                    tenant_id: str) -> list[dict]:
    """强证据关键词检索（当前默认模式，也是 hybrid 的兜底通道）。"""
    terms = _terms(query)
    with _db() as conn:
        rows = conn.execute(
            f"SELECT * FROM policy_docs WHERE tenant_id=? AND {_effective_where()}",
            (tenant_id,),
        ).fetchall()
    if not terms:
        return [_row_to_clause(r) for r in rows[:limit]]
    scored = []
    for r in rows:
        curated = f'{r["title"] or ""} {r["keywords"] or ""}'
        body = r["text"] or ""
        total = 0
        for t in terms:
            ok, score = _evidence(t, curated, body)
            if ok:
                total += score
        if total:
            scored.append((total, r))
    scored.sort(key=lambda x: -x[0])
    return [_row_to_clause(r) for _, r in scored[:limit]]


def _vector_candidates(query: str, tenant_id: str,
                       limit: int = 8) -> list[tuple[tuple[str, str], float]]:
    """向量召回 → [(clause_id, version), similarity]；失败由调用方降级。"""
    from domains.policy import embedding, vector_index

    settings = get_settings()
    vec = embedding.embed_texts([query])[0]
    rows = vector_index.search(vec, tenant_id, limit=max(24, limit * 3))
    best: dict[tuple[str, str], float] = {}
    for row in rows:
        if row["similarity"] < settings.policy_vector_min_score:
            continue
        key = (row["clause_id"], row["version"])
        best[key] = max(best.get(key, 0.0), row["similarity"])
    ranked = sorted(best.items(), key=lambda kv: -kv[1])
    return ranked[:limit]


def _effective_rows(tenant_id: str) -> dict[tuple[str, str], sqlite3.Row]:
    with _db() as conn:
        rows = conn.execute(
            f"SELECT * FROM policy_docs WHERE tenant_id=? AND {_effective_where()}",
            (tenant_id,),
        ).fetchall()
    return {(r["clause_id"], r["version"]): r for r in rows}


def search_clauses(query: str, limit: int = 3, *, tenant_id: str) -> list[dict]:
    """政策条款检索：keyword（默认）或 hybrid（关键词 + pgvector + RRF）。

    签名保持不变；PG/模型不可用时自动降级 keyword，不阻断作答。
    """
    settings = get_settings()
    keyword_hits = _search_keyword(query, limit=5, tenant_id=tenant_id)
    if settings.policy_retriever != "hybrid":
        return keyword_hits[:limit]
    try:
        vector_hits = _vector_candidates(query, tenant_id)
    except Exception as exc:  # noqa: BLE001 - 降级而非失败
        logger.warning("政策向量检索不可用，降级 keyword：%s", exc)
        return keyword_hits[:limit]
    if not vector_hits:
        return keyword_hits[:limit]

    scores: dict[tuple[str, str], float] = {}
    for rank, clause in enumerate(keyword_hits, start=1):
        key = (clause["clause_id"], clause["version"])
        scores[key] = scores.get(key, 0.0) + 1.0 / (60 + rank)
    for rank, (key, _sim) in enumerate(vector_hits, start=1):
        scores[key] = scores.get(key, 0.0) + 1.0 / (60 + rank)

    rows = _effective_rows(tenant_id)
    ordered = sorted(scores, key=lambda k: -scores[k])
    out = [rows[key] for key in ordered if key in rows]
    if not out:
        return keyword_hits[:limit]
    return [_row_to_clause(r) for r in out[:limit]]


def get_effective_clause(clause_id: str, *, tenant_id: str) -> dict | None:
    """按条款号取当前生效版（同号多版本时取生效期最新的）。"""
    with _db() as conn:
        rows = conn.execute(
            f"SELECT * FROM policy_docs WHERE tenant_id=? AND clause_id=? "
            f"AND {_effective_where()} ORDER BY effective_from DESC",
            (tenant_id, clause_id),
        ).fetchall()
    return _row_to_clause(rows[0]) if rows else None


def list_effective(*, tenant_id: str) -> list[dict]:
    with _db() as conn:
        rows = conn.execute(
            f"SELECT * FROM policy_docs WHERE tenant_id=? AND {_effective_where()} "
            "ORDER BY clause_id, effective_from DESC",
            (tenant_id,),
        ).fetchall()
    return [_row_to_clause(r) for r in rows]


def search_knowledge(
    query: str, limit: int = 3, *, tenant_id: str, status: str = "published",
) -> list[dict]:
    """knowledge_entries 检索（补充知识，非权威条款）。

    status="all" 供坐席工作台查看草稿/待审/已发布全部版本。
    """
    today = date.today().isoformat()
    sql = (
        "SELECT entry_id, version, title, content, scope, status, "
        "effective_from, effective_to, keywords, conflicts_with FROM knowledge_entries "
        "WHERE tenant_id=? "
        "AND (effective_from IS NULL OR effective_from='' OR effective_from<=?) "
        "AND (effective_to IS NULL OR effective_to='' OR effective_to>=?)"
    )
    args: list = [tenant_id, today, today]
    if status and status != "all":
        sql += " AND status=?"
        args.append(status)
    with _db() as conn:
        rows = conn.execute(sql, tuple(args)).fetchall()
    terms = _terms(query)
    if not terms:
        return []
    scored = []
    for row in rows:
        entry = dict(row)
        curated = " ".join(str(entry.get(k) or "") for k in ("title", "keywords"))
        body = " ".join(str(entry.get(k) or "") for k in ("content", "scope"))
        total = 0
        for t in terms:
            ok, score = _evidence(t, curated, body)
            if ok:
                total += score
        if total > 0:
            entry["score"] = total
            scored.append(entry)
    scored.sort(key=lambda e: e["score"], reverse=True)
    return scored[:limit]
