"""结算与财务域工具（技术方案第 6 章 skill 13-16）。

- billing.statement：按订单号查实付账单（读；退款金额以财务账单为准，3.4）
- refund.status：查退款单状态（读）
- refund.settle：执行退款（写；金额阈值审批 + biz_key 幂等 + chaos 注入）
- invoice.issue：开票（写；金额阈值审批 + 两阶段确认 + 幂等）

全部 SQL 强制 tenant_id 过滤；高价值动作先走 shared.approvals 审批/双人复核。
"""

import json
import logging
import secrets
import sqlite3
from pathlib import Path

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from shared.db import business_db_path
from shared.approvals import (
    consume, create_approval, find_approved, find_pending,
    supersede_siblings,
)
from shared.audit import audit_from_config
from shared.auth import auth_from_config, ensure_scope
from shared.authz import AuthzError, require_order, resolve_customer
from shared.confirmations import consume_confirmation, create_confirmation, get_confirmation
from shared.risk import assess
from shared.tenant import tenant_id

logger = logging.getLogger(__name__)




def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(business_db_path())
    conn.row_factory = sqlite3.Row
    return conn


def _rejected(scope: str) -> str:
    return json.dumps({"error": "rejected", "need_scope": scope}, ensure_ascii=False)


def _pending(decision, approval: dict, extra: dict | None = None) -> str:
    payload = {
        "status": "pending_approval",
        "approval_id": approval.get("id"),
        "required_approvals": decision.required_approvals,
        "dual_control": decision.dual_control,
        "approver_roles": list(decision.approver_roles),
        "amount": approval.get("amount"),
        "target": approval.get("target"),
        "expires_at": approval.get("expires_at"),
        "reason": decision.reason,
        "hint": "需主管/财务审批；审批通过后带 approval_id 重新调用",
    }
    payload.update(extra or {})
    return json.dumps(payload, ensure_ascii=False)


@tool
def billing_statement(order_no: str, config: RunnableConfig) -> str:
    """查询订单实付账单（skill: billing.statement）。order_no 如 SO-2001。"""
    if not ensure_scope(config, "finance.read"):
        return _rejected("finance.read")
    tenant = tenant_id(config)
    no = (order_no or "").strip().upper()
    if not no:
        return json.dumps({"error": "need_order_no"}, ensure_ascii=False)
    try:
        cust = resolve_customer(config)
    except AuthzError as e:
        return e.as_json()
    with _db() as conn:
        try:
            row = require_order(conn, tenant, no, cust)
        except AuthzError as e:
            return e.as_json()
    if row is None:
        return json.dumps({"error": "not_found", "order_no": no}, ensure_ascii=False)
    d = dict(row)
    if not d.get("paid_at"):
        return json.dumps(
            {"error": "unpaid", "order_no": no, "status": d["status"],
             "hint": "订单未支付，无实付账单"},
            ensure_ascii=False,
        )
    return json.dumps(d, ensure_ascii=False)


@tool
def invoice_status_query(order_no: str, config: RunnableConfig) -> str:
    """查询开票进度（skill: invoice.status）。order_no 可空，空则查当前客户全部发票。"""
    if not ensure_scope(config, "finance.read"):
        return _rejected("finance.read")
    tenant = tenant_id(config)
    no = (order_no or "").strip().upper()
    try:
        cust = resolve_customer(config)
    except AuthzError as e:
        return e.as_json()
    with _db() as conn:
        if no:
            try:
                require_order(conn, tenant, no, cust)
            except AuthzError as e:
                return e.as_json()
            rows = conn.execute(
                "SELECT invoice_id, order_no, title, status, created_at "
                "FROM invoices WHERE tenant_id=? AND order_no=? "
                "ORDER BY created_at DESC",
                (tenant, no),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT i.invoice_id, i.order_no, i.title, i.status, i.created_at "
                "FROM invoices i JOIN orders o "
                "ON o.tenant_id=i.tenant_id AND o.order_no=i.order_no "
                "WHERE i.tenant_id=? AND o.customer_id=? "
                "ORDER BY i.created_at DESC LIMIT 20",
                (tenant, cust),
            ).fetchall()
    if not rows:
        return json.dumps(
            {"invoices": [], "answer": "暂无开票记录",
             "hint": "可在对话中回复「给订单 XXX 开票」发起"},
            ensure_ascii=False,
        )
    return json.dumps(
        {"invoices": [dict(r) for r in rows], "answer": "已找到开票记录"},
        ensure_ascii=False,
    )


@tool
def refund_status_query(order_no: str, config: RunnableConfig) -> str:
    """查询退款单状态（skill: refund.status）。order_no 如 SO-2004。"""
    if not ensure_scope(config, "finance.read"):
        return _rejected("finance.read")
    tenant = tenant_id(config)
    no = (order_no or "").strip().upper()
    try:
        cust = resolve_customer(config)
    except AuthzError as e:
        return e.as_json()
    with _db() as conn:
        try:
            require_order(conn, tenant, no, cust)
        except AuthzError as e:
            return e.as_json()
        rows = conn.execute(
            "SELECT * FROM refunds WHERE tenant_id=? AND order_no=? "
            "ORDER BY refund_id DESC",
            (tenant, no),
        ).fetchall()
    if not rows:
        return json.dumps(
            {"error": "no_refund", "order_no": no, "hint": "该订单无退款单"},
            ensure_ascii=False,
        )
    return json.dumps([dict(r) for r in rows], ensure_ascii=False)


@tool
def refund_settle(order_no: str, config: RunnableConfig, approval_id: str = "") -> str:
    """执行退款（skill: refund.settle，写操作）。

    金额以财务账单实付为准（3.4）；超过阈值需审批/双人复核；biz_key 幂等；
    metadata.chaos=finance_settle_fail 时注入故障（Saga 演练用）。
    """
    if not ensure_scope(config, "finance.write"):
        return _rejected("finance.write")
    tenant = tenant_id(config)
    no = (order_no or "").strip().upper()
    if not no:
        return json.dumps({"error": "need_order_no"}, ensure_ascii=False)
    try:
        cust = resolve_customer(config)
    except AuthzError as e:
        return e.as_json()
    meta = (config or {}).get("metadata") or {}
    auth = auth_from_config(config)
    ctx = meta.get("context_id") or meta.get("trace_id") or "adhoc"
    biz_key = f"{ctx}:{no}:refund"

    with _db() as conn:
        try:
            bill = require_order(conn, tenant, no, cust)
        except AuthzError as e:
            return e.as_json()
    if not bill["paid_at"]:
        return json.dumps(
            {"error": "no_bill", "order_no": no,
             "hint": "无实付账单，不能退款（3.4：金额以财务为准，无数据即拒答）"},
            ensure_ascii=False,
        )
    amount = float(bill["amount"])
    with _db() as conn:
        existing_refund = conn.execute(
            "SELECT * FROM refunds WHERE tenant_id=? AND "
            "(biz_key=? OR (order_no=? AND status='settled')) "
            "ORDER BY created_at DESC LIMIT 1",
            (tenant, biz_key, no),
        ).fetchone()
    if existing_refund is not None:
        # 退款已结算：联动关闭同订单其余审批单（superseded），避免悬空待办
        try:
            supersede_siblings(
                tenant, action="refund", target=no,
                reason="退款已结算，联动关闭同订单审批单",
                actor_id=auth.actor_id if auth else "system",
                role=auth.role if auth else "service",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("同订单审批单联动关闭失败（不影响已结算）：order=%s %s", no, exc)
        return json.dumps(
            {"status": "settled", "refund_id": existing_refund["refund_id"],
             "order_no": no, "amount": existing_refund["amount"],
             "biz_key": existing_refund["biz_key"], "idempotent": True,
             "verified": existing_refund["status"] == "settled"},
            ensure_ascii=False,
        )
    if meta.get("chaos") == "finance_settle_fail":
        return json.dumps(
            {"error": "chaos_injected", "order_no": no,
             "hint": "模拟财务域故障（chaos 注入），正向步骤失败，等待编排层补偿"},
            ensure_ascii=False,
        )

    decision = assess("refund.settle", amount)
    if decision.requires_approval:
        if not approval_id:
            existing = find_approved(
                tenant, action="refund", target=no, amount=amount,
                requested_by=auth.actor_id if auth else "unknown",
            )
            if existing:
                approval_id = str(existing["id"])
        if not approval_id:
            # M4 补充：同订单已有待审批退款单 → 幂等返回同一张，不重复创建
            pending = find_pending(tenant, action="refund", target=no)
            if pending is not None:
                return json.dumps(
                    {"status": "pending_approval", "approval_id": pending["id"],
                     "required_approvals": pending.get("required_approvals"),
                     "dual_control": bool(pending.get("dual_control")),
                     "amount": pending.get("amount"),
                     "target": pending.get("target"),
                     "order_no": no, "duplicate": True,
                     "expires_at": pending.get("expires_at"),
                     "hint": "同订单已存在待审批退款单（幂等返回），请勿重复提交"},
                    ensure_ascii=False,
                )
            approval = create_approval(
                tenant_id=tenant, action="refund", target=no, amount=amount,
                requested_by=auth.actor_id if auth else "unknown",
                requested_role=auth.role if auth else "unknown",
                required_approvals=decision.required_approvals,
                approver_roles=decision.approver_roles,
                dual_control=decision.dual_control,
                reason=decision.reason,
                context_id=str(meta.get("context_id", "")),
                task_id=str(meta.get("task_id", "")),
                conversation_id=str(meta.get("context_id", "")),
                resume={
                    "intent": "refund_status",
                    "slots": {"order_no": no},
                    "customer_id": cust,
                },
            )
            return _pending(decision, approval, {"order_no": no})
        pending_consume = True
    else:
        pending_consume = False

    audit_from_config(
        config, "tool.refund_settle", target=f"refund:{no}",
        params={"order_no": no, "amount": amount, "approval_id": approval_id or None},
        context_id=str(meta.get("context_id", "")),
        task_id=str(meta.get("task_id", "")),
    )
    if pending_consume:
        consumed = consume(
            tenant, approval_id, action="refund", target=no, amount=amount,
            actor_id=auth.actor_id if auth else "unknown",
            role=auth.role if auth else "unknown",
        )
        if not consumed.get("ok"):
            return json.dumps(
                {"error": "approval_required", "detail": consumed.get("error"),
                 "approval_id": approval_id},
                ensure_ascii=False,
            )

    with _db() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO refunds(tenant_id, refund_id, order_no, biz_key, "
            "amount, status) VALUES (?,?,?,?,?,'settled')",
            (tenant, f"RF-{secrets.token_hex(8)}", no, biz_key, amount),
        )
        inserted = cur.rowcount == 1
        if inserted:
            row = conn.execute(
                "SELECT * FROM refunds WHERE tenant_id=? AND biz_key=?",
                (tenant, biz_key),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM refunds WHERE tenant_id=? AND order_no=? "
                "AND status='settled'",
                (tenant, no),
            ).fetchone()
        conn.commit()
    audit_from_config(
        config, "tool.refund_settle.result", target=f"refund:{row['refund_id']}",
        result_summary=f"settled amount={row['amount']} idempotent={not inserted}",
        context_id=str(meta.get("context_id", "")),
    )
    try:
        supersede_siblings(
            tenant, action="refund", target=no,
            except_id=str(approval_id) if approval_id else None,
            reason="退款已结算，联动关闭同订单审批单",
            actor_id=auth.actor_id if auth else "system",
            role=auth.role if auth else "service",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("同订单审批单联动关闭失败（不影响已结算）：order=%s %s", no, exc)
    return json.dumps(
        {"status": "settled", "refund_id": row["refund_id"], "order_no": no,
         "amount": row["amount"], "biz_key": row["biz_key"],
         "idempotent": not inserted,
         "verified": row["status"] == "settled"},
        ensure_ascii=False,
    )


@tool
def invoice_issue(
    order_no: str,
    title: str,
    config: RunnableConfig,
    confirm_token: str = "",
    approval_id: str = "",
) -> str:
    """开具发票（skill: invoice.issue，写操作；金额阈值审批 + 两阶段确认 + 幂等）。"""
    if not ensure_scope(config, "finance.write"):
        return _rejected("finance.write")
    tenant = tenant_id(config)
    no = (order_no or "").strip().upper()
    t = (title or "").strip()
    if not no or not t:
        return json.dumps(
            {"error": "missing_params", "hint": "需要 order_no 与 title（发票抬头）"},
            ensure_ascii=False,
        )
    meta = (config or {}).get("metadata") or {}
    auth = auth_from_config(config)
    ctx = meta.get("context_id") or meta.get("trace_id") or "adhoc"
    biz_key = f"{ctx}:{no}:invoice"
    try:
        cust = resolve_customer(config)
    except AuthzError as e:
        return e.as_json()

    with _db() as conn:
        try:
            bill = require_order(conn, tenant, no, cust)
        except AuthzError as e:
            return e.as_json()
    if not bill["paid_at"]:
        return json.dumps({"error": "no_bill", "order_no": no}, ensure_ascii=False)
    amount = float(bill["amount"])
    with _db() as conn:
        existing_invoice = conn.execute(
            "SELECT * FROM invoices WHERE tenant_id=? AND "
            "(biz_key=? OR (order_no=? AND status='issued')) "
            "ORDER BY created_at DESC LIMIT 1",
            (tenant, biz_key, no),
        ).fetchone()
    if existing_invoice is not None:
        if confirm_token:
            payload = {"order_no": no, "title": t, "amount": amount}
            conf = get_confirmation(confirm_token, tenant_id=tenant)
            try:
                same_payload = json.loads(conf.get("payload_json") or "{}") == payload
            except json.JSONDecodeError:
                same_payload = False
            valid_scope = bool(
                conf
                and conf.get("action") == "invoice.issue"
                and conf.get("target") == no
                and (conf.get("customer_id") or "") == (cust or "")
                and conf.get("actor_id") == auth_from_config(config).actor_id
                and same_payload
            )
            if not valid_scope or conf.get("status") == "expired":
                return json.dumps(
                    {"error": "invalid_confirm_token",
                     "detail": "confirmation_scope_mismatch"},
                    ensure_ascii=False)
            if conf.get("status") == "pending":
                consumed = consume_confirmation(
                    confirm_token, tenant_id=tenant,
                    actor_id=auth_from_config(config).actor_id,
                    customer_id=cust, action="invoice.issue", target=no,
                    payload=payload,
                )
                if not consumed.get("ok"):
                    return json.dumps(
                        {"error": "invalid_confirm_token", "detail": consumed.get("error")},
                        ensure_ascii=False)
        return json.dumps(
            {"status": "issued", "invoice_id": existing_invoice["invoice_id"],
             "order_no": no, "title": existing_invoice["title"],
             "amount": amount, "idempotent": True, "verified": True},
            ensure_ascii=False,
        )
    decision = assess("invoice.issue", amount)
    if decision.requires_approval:
        if not approval_id:
            existing = find_approved(
                tenant, action="invoice", target=no, amount=amount,
                requested_by=auth.actor_id if auth else "unknown",
            )
            if existing:
                approval_id = str(existing["id"])
        if not approval_id:
            approval = create_approval(
                tenant_id=tenant, action="invoice", target=no, amount=amount,
                requested_by=auth.actor_id if auth else "unknown",
                requested_role=auth.role if auth else "unknown",
                required_approvals=decision.required_approvals,
                approver_roles=decision.approver_roles,
                dual_control=decision.dual_control,
                reason=decision.reason,
                context_id=str(meta.get("context_id", "")),
                task_id=str(meta.get("task_id", "")),
                conversation_id=str(meta.get("context_id", "")),
                resume={
                    "intent": "invoice_status",
                    "slots": {"order_no": no, "title": t},
                    "customer_id": cust,
                },
            )
            return _pending(
                decision, approval,
                {"order_no": no, "title": t,
                 "preview": {"order_no": no, "title": t, "amount": amount}},
            )
        pending_consume = True
    else:
        pending_consume = False

    confirm_payload = {"order_no": no, "title": t, "amount": amount}
    if not confirm_token and not (decision.requires_approval and approval_id):
        confirmation = create_confirmation(
            tenant_id=tenant,
            actor_id=auth.actor_id if auth else "unknown",
            customer_id=cust,
            action="invoice.issue",
            target=no,
            payload=confirm_payload,
            context_id=str(meta.get("context_id", "")),
            task_id=str(meta.get("task_id", "")),
        )
        return json.dumps(
            {"status": "pending_confirm", "biz_key": biz_key,
             "confirm_token": confirmation["confirm_token"],
             "expires_at": confirmation["expires_at"],
             "preview": {"order_no": no, "title": t, "amount": amount},
             "hint": "确认无误后带 confirm_token 再次调用才会执行"},
            ensure_ascii=False,
        )
    if confirm_token and not (decision.requires_approval and approval_id):
        confirmed = consume_confirmation(
            confirm_token,
            tenant_id=tenant,
            actor_id=auth.actor_id if auth else "unknown",
            customer_id=cust,
            action="invoice.issue",
            target=no,
            payload=confirm_payload,
        )
        if not confirmed.get("ok"):
            return json.dumps(
                {"error": "invalid_confirm_token", "detail": confirmed.get("error")},
                ensure_ascii=False,
            )

    if pending_consume:
        consumed = consume(
            tenant, approval_id, action="invoice", target=no, amount=amount,
            actor_id=auth.actor_id if auth else "unknown",
            role=auth.role if auth else "unknown",
        )
        if not consumed.get("ok"):
            return json.dumps(
                {"error": "approval_required", "detail": consumed.get("error"),
                 "approval_id": approval_id},
                ensure_ascii=False,
            )

    with _db() as conn:
        audit_from_config(
            config, "tool.invoice_issue", target=f"invoice:{no}",
            params={"order_no": no, "amount": amount, "approval_id": approval_id or None},
            context_id=str(meta.get("context_id", "")),
            task_id=str(meta.get("task_id", "")),
        )
        cur = conn.execute(
            "INSERT OR IGNORE INTO invoices(tenant_id, invoice_id, order_no, biz_key, "
            "title, status) VALUES (?,?,?,?,?,'issued')",
            (tenant, f"INV-{secrets.token_hex(8)}", no, biz_key, t),
        )
        inserted = cur.rowcount == 1
        if inserted:
            row = conn.execute(
                "SELECT * FROM invoices WHERE tenant_id=? AND biz_key=?",
                (tenant, biz_key),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM invoices WHERE tenant_id=? AND order_no=? "
                "AND status='issued'",
                (tenant, no),
            ).fetchone()
        conn.commit()
    audit_from_config(
        config, "tool.invoice_issue.result", target=f"invoice:{row['invoice_id']}",
        result_summary=f"issued amount={amount} idempotent={not inserted}",
        context_id=str(meta.get("context_id", "")),
    )
    return json.dumps(
        {"status": "issued", "invoice_id": row["invoice_id"], "order_no": no,
         "title": row["title"], "amount": amount,
         "idempotent": not inserted, "verified": row["status"] == "issued"},
        ensure_ascii=False,
    )


TOOLS = [billing_statement, refund_status_query, refund_settle, invoice_issue,
         invoice_status_query]
TOOL_TO_SKILL = {
    "billing_statement": "billing.statement",
    "refund_status_query": "refund.status",
    "refund_settle": "refund.settle",
    "invoice_issue": "invoice.issue",
    "invoice_status_query": "invoice.status",
}
