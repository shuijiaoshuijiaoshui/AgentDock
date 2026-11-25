"""订单域工具（技术方案第 6 章 skill 5-6）。

- order.query：按订单号 / 客户 id 查订单（只读）
- order.amend：改收货地址 / 改价（写操作；两阶段确认 + 高价值审批 + 幂等）
- order.cancel / order.restore：Saga 正向/补偿

全部 SQL 强制 tenant_id 过滤；tenant 来自 JWT claim（shared.tenant）。
"""

import json
import math
import secrets
import sqlite3
from pathlib import Path

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from shared.db import business_db_path
from shared.approvals import consume, create_approval, find_approved
from shared.audit import audit_from_config
from shared.auth import auth_from_config, ensure_scope
from shared.authz import (
    AuthzError,
    require_customer_exists,
    require_order,
    resolve_customer,
)
from shared.confirmations import consume_confirmation, create_confirmation
from shared.risk import assess
from shared.tenant import tenant_id



# 订单状态中文名（对外统一口径）
STATUS_NAMES = {
    "pending_payment": "待付款",
    "paid": "已付款",
    "shipped": "已发货",
    "in_transit": "运输中",
    "signed": "已签收",
    "cancelled": "已取消",
}


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(business_db_path())
    conn.row_factory = sqlite3.Row
    return conn


def _rejected(scope: str, config) -> str:  # noqa: ANN001
    return json.dumps({"error": "rejected", "need_scope": scope}, ensure_ascii=False)


def _pending_approval(decision, approval: dict) -> str:
    return json.dumps(
        {
            "status": "pending_approval",
            "approval_id": approval.get("id"),
            "required_approvals": decision.required_approvals,
            "dual_control": decision.dual_control,
            "approver_roles": list(decision.approver_roles),
            "amount": approval.get("amount"),
            "order_no": approval.get("target"),
            "preview": {"order_no": approval.get("target"),
                        "new_price": approval.get("amount")},
            "expires_at": approval.get("expires_at"),
            "reason": decision.reason,
            "hint": "需主管/财务审批；审批通过后带 approval_id 重新调用",
        },
        ensure_ascii=False,
    )


@tool
def order_query(order_no: str, config: RunnableConfig, customer_id: str = "") -> str:
    """查询订单状态（skill: order.query）。order_no 可空，空则按客户查最近订单。"""
    if not ensure_scope(config, "order.read"):
        return _rejected("order.read", config)
    tenant = tenant_id(config)
    try:
        cust = resolve_customer(config, customer_id)
    except AuthzError as e:
        return e.as_json()
    with _db() as conn:
        try:
            require_customer_exists(conn, tenant, cust)
        except AuthzError as e:
            return e.as_json()
        if order_no:
            rows = conn.execute(
                "SELECT * FROM orders WHERE tenant_id=? AND order_no=? "
                "AND customer_id=?",
                (tenant, order_no.strip().upper(), cust),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM orders WHERE tenant_id=? AND customer_id=? "
                "ORDER BY created_at DESC",
                (tenant, cust),
            ).fetchall()
    if not rows:
        return json.dumps(
            {"error": "not_found", "order_no": order_no, "customer_id": cust},
            ensure_ascii=False,
        )
    out = []
    for r in rows:
        d = dict(r)
        d["status_name"] = STATUS_NAMES.get(d.get("status", ""), d.get("status"))
        out.append(d)
    return json.dumps(out if len(out) > 1 else out[0], ensure_ascii=False)


@tool
def order_amend(
    order_no: str,
    address: str,
    config: RunnableConfig,
    confirm_token: str = "",
    new_price: float | None = None,
    approval_id: str = "",
) -> str:
    """修改订单收货地址或改价（skill: order.amend，写操作）。

    安全机制：tenant 过滤 + 两阶段确认 + 高价值审批（改价金额超阈值）；
    已发货/已签收订单不可改。
    """
    if not ensure_scope(config, "order.write"):
        return _rejected("order.write", config)
    tenant = tenant_id(config)
    no = (order_no or "").strip().upper()
    new_addr = (address or "").strip()
    if not no or (not new_addr and new_price is None):
        return json.dumps(
            {"error": "missing_params", "hint": "需要 order_no 与 address/new_price"},
            ensure_ascii=False,
        )
    if new_price is not None:
        try:
            price = float(new_price)
        except (TypeError, ValueError):
            return json.dumps({"error": "invalid_price"}, ensure_ascii=False)
        if not math.isfinite(price) or price <= 0:
            return json.dumps(
                {"error": "invalid_price", "hint": "改价必须为正数"},
                ensure_ascii=False,
            )
        new_price = price
    try:
        cust = resolve_customer(config)
    except AuthzError as e:
        return e.as_json()
    meta = (config or {}).get("metadata") or {}
    auth = auth_from_config(config)
    biz_key = f"{meta.get('context_id', '')}:{no}:amend"
    audit_from_config(
        config, "tool.order_amend",
        target=f"order:{no}",
        params={"order_no": no, "new_price": new_price, "address": bool(new_addr)},
        context_id=str(meta.get("context_id", "")),
        task_id=str(meta.get("task_id", "")),
    )

    # 先做对象归属/状态校验，再评估金额与创建审批：
    # 客户 A 不能通过“高价改价”在 B 的订单上留下审批单。
    with _db() as conn:
        try:
            require_customer_exists(conn, tenant, cust)
            row = require_order(conn, tenant, no, cust)
        except AuthzError as e:
            return e.as_json()
    status = row["status"]
    if status not in ("pending_payment", "paid"):
        return json.dumps(
            {"error": "not_amendable", "order_no": no,
             "status": status, "status_name": STATUS_NAMES.get(status, status),
             "hint": "订单已发货或已完成，无法修改，请联系人工客服"},
            ensure_ascii=False,
        )

    decision = assess("order.amend", new_price) if new_price is not None else None
    pending_consume = False
    if decision and decision.requires_approval:
        if not approval_id:
            existing = find_approved(
                tenant, action="price_change", target=no, amount=float(new_price),
                requested_by=auth.actor_id if auth else "unknown",
            )
            if existing:
                approval_id = str(existing["id"])
        if not approval_id:
            approval = create_approval(
                tenant_id=tenant,
                action="price_change",
                target=no,
                amount=float(new_price),
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
                    "intent": "order_price_amend",
                    "slots": {"order_no": no, "new_price": new_price},
                    "customer_id": cust,
                },
            )
            return _pending_approval(decision, approval)
        pending_consume = True

    confirm_payload = {
        "order_no": no, "address": new_addr, "new_price": new_price,
    }
    if not confirm_token and not (decision and decision.requires_approval):
        confirmation = create_confirmation(
            tenant_id=tenant,
            actor_id=auth.actor_id if auth else "unknown",
            customer_id=cust,
            action="order.amend",
            target=no,
            payload=confirm_payload,
            context_id=str(meta.get("context_id", "")),
            task_id=str(meta.get("task_id", "")),
        )
        return json.dumps(
            {"status": "pending_confirm", "biz_key": biz_key,
             "confirm_token": confirmation["confirm_token"],
             "expires_at": confirmation["expires_at"],
             "preview": {"order_no": no, "old_address": row["address"],
                         "new_address": new_addr, "new_price": new_price},
             "hint": "确认无误后带 confirm_token 再次调用才会执行"},
            ensure_ascii=False,
        )
    if confirm_token and not (decision and decision.requires_approval):
        confirmed = consume_confirmation(
            confirm_token,
            tenant_id=tenant,
            actor_id=auth.actor_id if auth else "unknown",
            customer_id=cust,
            action="order.amend",
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
            tenant, approval_id, action="price_change", target=no,
            amount=float(new_price), actor_id=auth.actor_id if auth else "unknown",
            role=auth.role if auth else "unknown",
        )
        if not consumed.get("ok"):
            return json.dumps(
                {"error": "approval_required", "detail": consumed.get("error"),
                 "approval_id": approval_id},
                ensure_ascii=False,
            )

    with _db() as conn:
        if new_price is not None:
            conn.execute(
                "UPDATE orders SET amount=?, address=? "
                "WHERE tenant_id=? AND order_no=? AND customer_id=?",
                (float(new_price), new_addr or row["address"], tenant, no, cust),
            )
        else:
            conn.execute(
                "UPDATE orders SET address=? "
                "WHERE tenant_id=? AND order_no=? AND customer_id=?",
                (new_addr, tenant, no, cust),
            )
        conn.commit()
        verified = conn.execute(
            "SELECT address, amount FROM orders "
            "WHERE tenant_id=? AND order_no=? AND customer_id=?",
            (tenant, no, cust),
        ).fetchone()
    return json.dumps(
        {
            "status": "amended", "order_no": no,
            "address": verified["address"],
            "amount": verified["amount"],
            "verified": (
                (new_price is None or abs(float(verified["amount"]) - float(new_price)) < 0.01)
                and (not new_addr or verified["address"] == new_addr)
            ),
        },
        ensure_ascii=False,
    )


@tool
def cancel_order(order_no: str, reason: str, config: RunnableConfig) -> str:
    """取消订单（skill: order.cancel，写操作，Saga 正向步骤）。"""
    if not ensure_scope(config, "order.write"):
        return _rejected("order.write", config)
    tenant = tenant_id(config)
    no = (order_no or "").strip().upper()
    if not no:
        return json.dumps({"error": "need_order_no"}, ensure_ascii=False)
    try:
        cust = resolve_customer(config)
    except AuthzError as e:
        return e.as_json()
    meta = (config or {}).get("metadata") or {}
    ctx = meta.get("context_id") or meta.get("trace_id") or "adhoc"
    biz_key = f"{ctx}:{no}:cancel"
    audit_from_config(
        config, "tool.order_cancel", target=f"order:{no}", params={"order_no": no},
        context_id=str(meta.get("context_id", "")),
        task_id=str(meta.get("task_id", "")),
    )
    with _db() as conn:
        try:
            require_customer_exists(conn, tenant, cust)
            row = require_order(conn, tenant, no, cust)
        except AuthzError as e:
            return e.as_json()
        if row["status"] == "cancelled":
            return json.dumps(
                {"status": "cancelled", "order_no": no, "idempotent": True,
                 "biz_key": biz_key},
                ensure_ascii=False,
            )
        conn.execute(
            "INSERT OR IGNORE INTO order_cancel_log"
            "(tenant_id, log_id, order_no, biz_key, prev_status) VALUES (?,?,?,?,?)",
            (tenant, f"CL-{secrets.token_hex(8)}", no, biz_key, row["status"]),
        )
        conn.execute(
            "UPDATE orders SET status='cancelled' "
            "WHERE tenant_id=? AND order_no=? AND customer_id=?",
            (tenant, no, cust),
        )
        conn.commit()
        verified = conn.execute(
            "SELECT status FROM orders "
            "WHERE tenant_id=? AND order_no=? AND customer_id=?",
            (tenant, no, cust),
        ).fetchone()["status"]
    audit_from_config(
        config, "tool.order_cancel.result", target=f"order:{no}",
        result_summary=f"verified={verified == 'cancelled'}",
        context_id=str(meta.get("context_id", "")),
    )
    return json.dumps(
        {"status": "cancelled", "order_no": no, "prev_status": row["status"],
         "reason": reason, "biz_key": biz_key, "verified": verified == "cancelled"},
        ensure_ascii=False,
    )


@tool
def restore_order(biz_key: str, config: RunnableConfig) -> str:
    """恢复订单到取消前状态（skill: order.restore，Saga 补偿动作，幂等）。"""
    if not ensure_scope(config, "order.write"):
        return _rejected("order.write", config)
    tenant = tenant_id(config)
    key = (biz_key or "").strip()
    if not key:
        return json.dumps({"error": "need_biz_key"}, ensure_ascii=False)
    try:
        cust = resolve_customer(config)
    except AuthzError as e:
        return e.as_json()
    with _db() as conn:
        log = conn.execute(
            "SELECT l.* FROM order_cancel_log l "
            "JOIN orders o ON o.tenant_id=l.tenant_id AND o.order_no=l.order_no "
            "WHERE l.tenant_id=? AND l.biz_key=? AND o.customer_id=?",
            (tenant, key, cust),
        ).fetchone()
        if log is None:
            return json.dumps({"error": "no_cancel_log", "biz_key": key},
                              ensure_ascii=False)
        cur_status = conn.execute(
            "SELECT status FROM orders "
            "WHERE tenant_id=? AND order_no=? AND customer_id=?",
            (tenant, log["order_no"], cust),
        ).fetchone()["status"]
        # 补偿只能撤销“未退款”的取消；已退款订单恢复会造成钱货两空
        refunded = conn.execute(
            "SELECT 1 FROM refunds WHERE tenant_id=? AND order_no=? "
            "AND status='settled' LIMIT 1",
            (tenant, log["order_no"]),
        ).fetchone()
        if refunded is not None:
            return json.dumps(
                {"error": "refund_settled", "order_no": log["order_no"],
                 "hint": "该订单已有成功退款，不允许恢复订单"},
                ensure_ascii=False,
            )
        if cur_status != "cancelled":
            return json.dumps(
                {"status": "restored", "order_no": log["order_no"],
                 "restored_to": cur_status, "idempotent": True},
                ensure_ascii=False,
            )
        conn.execute(
            "UPDATE orders SET status=? "
            "WHERE tenant_id=? AND order_no=? AND customer_id=?",
            (log["prev_status"], tenant, log["order_no"], cust),
        )
        conn.commit()
        verified = conn.execute(
            "SELECT status FROM orders "
            "WHERE tenant_id=? AND order_no=? AND customer_id=?",
            (tenant, log["order_no"], cust),
        ).fetchone()["status"]
    return json.dumps(
        {"status": "restored", "order_no": log["order_no"],
         "restored_to": verified, "biz_key": key, "verified": verified == log["prev_status"]},
        ensure_ascii=False,
    )


TOOLS = [order_query, order_amend, cancel_order, restore_order]
TOOL_TO_SKILL = {
    "order_query": "order.query",
    "order_amend": "order.amend",
    "cancel_order": "order.cancel",
    "restore_order": "order.restore",
}
