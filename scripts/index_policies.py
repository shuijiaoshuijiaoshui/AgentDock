"""政策向量索引 CLI：全量重建 / 消费发布 outbox / 状态。

用法：
    uv run python scripts/index_policies.py --rebuild [--tenant t_demo]
    uv run python scripts/index_policies.py --drain
    uv run python scripts/index_policies.py --status

依赖：pgvector 可达 + embedding 可用（local 需 `uv sync --extra embeddings`）。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from domains.policy import embedding, vector_index  # noqa: E402
from domains.policy.chunking import clause_chunks  # noqa: E402
from shared.config import get_settings  # noqa: E402
from shared.db import connect_business_db  # noqa: E402


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _audit(tenant: str, summary: str, params: dict) -> None:
    try:
        from shared.audit import audit_event

        audit_event(
            tenant_id=tenant, actor_id="system", actor_role="service",
            action="policy.index_sync", target=f"tenant:{tenant}",
            result_summary=summary, params=params, strict=False,
        )
    except Exception as exc:  # noqa: BLE001 - CLI 不因审计不可用而丢索引结果
        print(f"[warn] 审计写入失败（{tenant}）：{exc}", file=sys.stderr)


def _index_row(tenant: str, row, model: str) -> int:  # noqa: ANN001
    chunks = clause_chunks(row["title"] or "", row["text"] or "",
                           int(get_settings().policy_chunk_max_chars))
    vectors = embedding.embed_texts([c["text"] for c in chunks])
    return vector_index.upsert_clause(
        tenant, row["clause_id"], row["version"], chunks, vectors,
        model_name=model, title=row["title"] or "",
        keywords=row["keywords"] or "",
        effective_from=row["effective_from"],
        effective_to=row["effective_to"] or None,
    )


def rebuild(tenant_filter: str = "") -> int:
    model = get_settings().embedding_model
    with connect_business_db() as conn:
        tenants = [r["tenant_id"] for r in conn.execute(
            "SELECT DISTINCT tenant_id FROM policy_docs").fetchall()]
        if tenant_filter:
            tenants = [t for t in tenants if t == tenant_filter]
        total = 0
        for tenant in tenants:
            rows = conn.execute(
                "SELECT * FROM policy_docs WHERE tenant_id=? "
                "ORDER BY clause_id, effective_from", (tenant,),
            ).fetchall()
            deleted = vector_index.clear_tenant(tenant)
            count = 0
            for row in rows:
                count += _index_row(tenant, row, model)
            total += count
            _audit(tenant, f"rebuild chunks={count} deleted={deleted}",
                   {"mode": "rebuild", "chunks": count, "deleted": deleted,
                    "model": model})
            print(f"[rebuild] {tenant}: clauses={len(rows)} chunks={count} "
                  f"deleted={deleted}")
    print(f"[rebuild] 完成：chunks={total}")
    return 0


def drain(limit: int = 100) -> int:
    model = get_settings().embedding_model
    failures = 0
    done = 0
    with connect_business_db() as conn:
        items = conn.execute(
            "SELECT * FROM policy_index_outbox "
            "WHERE status IN ('pending','failed') AND attempts < 3 "
            "ORDER BY created_at LIMIT ?", (limit,),
        ).fetchall()
        for item in items:
            tenant = item["tenant_id"]
            row = conn.execute(
                "SELECT * FROM policy_docs WHERE tenant_id=? AND clause_id=? "
                "AND version=?",
                (tenant, item["clause_id"], item["version"]),
            ).fetchone()
            if row is None:
                conn.execute(
                    "UPDATE policy_index_outbox SET status='failed', "
                    "attempts=attempts+1, last_error=?, updated_at=? "
                    "WHERE tenant_id=? AND clause_id=? AND version=?",
                    ("source_missing", _now(), tenant, item["clause_id"],
                     item["version"]),
                )
                failures += 1
                continue
            try:
                _index_row(tenant, row, model)
                conn.execute(
                    "UPDATE policy_index_outbox SET status='done', "
                    "attempts=attempts+1, last_error=NULL, updated_at=? "
                    "WHERE tenant_id=? AND clause_id=? AND version=?",
                    (_now(), tenant, item["clause_id"], item["version"]),
                )
                done += 1
                print(f"[drain] {tenant} {item['clause_id']}@{item['version']} ok")
            except Exception as exc:  # noqa: BLE001
                conn.execute(
                    "UPDATE policy_index_outbox SET status='failed', "
                    "attempts=attempts+1, last_error=?, updated_at=? "
                    "WHERE tenant_id=? AND clause_id=? AND version=?",
                    (str(exc)[:300], _now(), tenant, item["clause_id"],
                     item["version"]),
                )
                failures += 1
                print(f"[drain] {tenant} {item['clause_id']}@{item['version']} "
                      f"失败：{exc}", file=sys.stderr)
        conn.commit()
    print(f"[drain] done={done} failed={failures} pending={len(items) - done - failures}")
    return 0 if failures == 0 else 1


def status(tenant_filter: str = "") -> int:
    info = vector_index.status(tenant_filter)
    with connect_business_db() as conn:
        rows = conn.execute(
            "SELECT status, count(*) AS n FROM policy_index_outbox "
            "GROUP BY status").fetchall()
    out = {"vector_index": info,
           "outbox": {r["status"]: r["n"] for r in rows}}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--drain", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--tenant", default="")
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    if not (args.rebuild or args.drain or args.status):
        parser.print_help()
        return 2
    rc = 0
    if args.rebuild:
        rc |= rebuild(args.tenant)
    if args.drain:
        rc |= drain(args.limit)
    if args.status:
        rc |= status(args.tenant)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
