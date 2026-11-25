"""三证据通道采集：执行轨迹（OTel span）/ 审计日志（HMAC 哈希链）/ 环境快照。

对应 Claw-Eval 的 evidence channels——评分依据不是"最终答案说了什么"，
而是"系统实际做了什么、留了什么痕、数据落成什么样"。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from shared.tracing import build_span_tree, read_trace_spans

# 环境快照覆盖的业务表（E 组/S 组写链的关键状态都在这里）
SNAPSHOT_TABLES = (
    "orders", "refunds", "invoices", "tickets", "order_cancel_log",
    "aftersales", "reconciliations",
)


def snapshot_business(db_path: str, tenant: str = "t_demo") -> dict:
    """按租户抓业务表快照（评分前后的 diff 用）。"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    out: dict[str, list[dict]] = {}
    try:
        for table in SNAPSHOT_TABLES:
            rows = conn.execute(
                f"SELECT * FROM {table} WHERE tenant_id=?", (tenant,)
            ).fetchall()
            out[table] = [dict(r) for r in rows]
    finally:
        conn.close()
    return out


def db_count(db_path: str, table: str, where: dict | None = None) -> int:
    sql = f"SELECT count(*) FROM {table}"  # noqa: S608（表名来自任务 YAML 白名单）
    args: list = []
    if where:
        sql += " WHERE " + " AND ".join(f"{k}=?" for k in where)
        args = list(where.values())
    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute(sql, tuple(args)).fetchone()[0])
    finally:
        conn.close()


def db_field(db_path: str, table: str, where: dict, field: str):
    sql = (f"SELECT {field} FROM {table} WHERE "  # noqa: S608
           + " AND ".join(f"{k}=?" for k in where))
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(sql, tuple(where.values())).fetchone()
        return None if row is None else row[field]
    finally:
        conn.close()


# -- 执行轨迹 ---------------------------------------------------------------

def spans_for(trace_ids: list[str], trace_dir: str) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for tid in trace_ids:
        if not tid or tid in seen:
            continue
        seen.add(tid)
        out.extend(read_trace_spans(tid, trace_dir))
    return out


def span_services(spans: list[dict]) -> set[str]:
    return {s.get("service", "") for s in spans if s.get("service")}


def span_tree(spans: list[dict]) -> dict:
    return build_span_tree(spans)


def services_in_tree(tree: dict, acc: set | None = None) -> set[str]:
    acc = set(acc or set())
    if not tree:
        return acc
    if tree.get("service"):
        acc.add(tree["service"])
    for child in tree.get("children") or []:
        acc |= services_in_tree(child, acc)
    return acc


# -- 审计日志（eval 进程内直读 security.db，模式为 local） ---------------------

def audit_events(tenant: str = "t_demo", trace_id: str = "",
                 limit: int = 500) -> list[dict]:
    from shared import audit

    return audit.list_events(tenant_id=tenant, trace_id=trace_id, limit=limit)


def audit_verify(tenant: str = "t_demo") -> dict:
    from shared import audit

    return audit.verify_chain(tenant)


def audit_trace_ids(events: list[dict]) -> set[str]:
    return {e.get("trace_id", "") for e in events if e.get("trace_id")}


# -- console.db（agent_runs / session_summaries） ------------------------------

def latest_agent_run_trace(console_db: str, conversation_id: str) -> str:
    """console 表面的 trace_id 从 agent_runs 取（与审计/span 同号）。"""
    conn = sqlite3.connect(console_db)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT trace_id FROM agent_runs WHERE conversation_id=? "
            "AND trace_id IS NOT NULL AND trace_id<>'' "
            "ORDER BY rowid DESC LIMIT 1",
            (conversation_id,),
        ).fetchone()
        return row["trace_id"] if row else ""
    finally:
        conn.close()


def session_summary(console_db: str, conversation_id: str) -> dict | None:
    conn = sqlite3.connect(console_db)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM session_summaries WHERE conversation_id=?",
            (conversation_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def bind_audit_db(security_db: str) -> None:
    """eval 进程内把 shared.audit 指到本套栈的 security.db（local 直读）。"""
    from shared.security_db import reset_for_tests

    reset_for_tests(security_db)


def diff_snapshot(before: dict, after: dict) -> dict:
    """报告用的前后差异摘要（每表行数 + 新增关键单据号）。"""
    out: dict[str, dict] = {}
    for table in after:
        b_ids = {_pk_of(table, r) for r in before.get(table, [])}
        a_ids = {_pk_of(table, r) for r in after.get(table, [])}
        added = [r for r in after.get(table, []) if _pk_of(table, r) not in b_ids]
        out[table] = {
            "before": len(before.get(table, [])),
            "after": len(after.get(table, [])),
            "added": added[:5],
        }
    return out


def _pk_of(table: str, row: dict) -> tuple:
    keys = {
        "orders": ("tenant_id", "order_no"),
        "refunds": ("tenant_id", "refund_id"),
        "invoices": ("tenant_id", "invoice_id"),
        "tickets": ("tenant_id", "ticket_no"),
        "order_cancel_log": ("tenant_id", "log_id"),
        "aftersales": ("tenant_id", "aftersale_id"),
        "reconciliations": ("tenant_id", "reconciliation_id"),
    }
    k = keys.get(table, ("tenant_id",))
    return tuple(row.get(x) for x in k)


def ensure_report_dir(base: Path) -> Path:
    d = base / "reports"
    d.mkdir(parents=True, exist_ok=True)
    return d
