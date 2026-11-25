"""履约域工具（技术方案第 6 章 skill 7-8）。

- fulfill.track：按订单号查物流轨迹（只读）
- fulfill.atp：可承诺库存/时效（ATP = Available-To-Promise，只读）

运单表 shipments + 轨迹表 shipment_nodes；无运单的订单（待付款）明确返回无物流。
"""

import json
import secrets
import sqlite3
from pathlib import Path
from typing import Any

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from shared.audit import audit_from_config
from shared.db import business_db_path
from shared.auth import ensure_scope
from shared.authz import AuthzError, resolve_customer
from shared.tenant import tenant_id



NODE_NAMES = {
    "collected": "已揽收",
    "in_transit": "运输中",
    "out_for_delivery": "派送中",
    "delivered": "已签收",
}


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(business_db_path())
    conn.row_factory = sqlite3.Row
    return conn


def _rejected(scope: str, config) -> str:  # noqa: ANN001
    return json.dumps(
        {"error": "rejected", "need_scope": scope}, ensure_ascii=False
    )


@tool
def fulfill_track(order_no: str, config: RunnableConfig) -> str:
    """查询订单物流轨迹（skill: fulfill.track）。order_no 可空，空则按会话客户查其最近运单。"""
    if not ensure_scope(config, "fulfillment.read"):
        return _rejected("fulfillment.read", config)
    tenant = tenant_id(config)
    no = (order_no or "").strip().upper()
    try:
        cust = resolve_customer(config)
    except AuthzError as e:
        return e.as_json()
    with _db() as conn:
        if not no:
            # 客户没报订单号：取该客户最近一笔已发货订单的运单（强制客户归属）
            row = conn.execute(
                "SELECT s.order_no FROM shipments s JOIN orders o "
                "ON o.tenant_id = s.tenant_id AND o.order_no = s.order_no "
                "WHERE s.tenant_id=? AND o.customer_id=? "
                "ORDER BY o.created_at DESC LIMIT 1",
                (tenant, cust),
            ).fetchone()
            no = row["order_no"] if row else ""
        if not no:
            return json.dumps(
                {"error": "need_order_no",
                 "hint": "请提供订单号（如 SO-2002）"},
                ensure_ascii=False,
            )
        ship = conn.execute(
            "SELECT s.* FROM shipments s JOIN orders o "
            "ON o.tenant_id = s.tenant_id AND o.order_no = s.order_no "
            "WHERE s.tenant_id=? AND s.order_no=? AND o.customer_id=?",
            (tenant, no, cust),
        ).fetchone()
        if ship is None:
            order = conn.execute(
                "SELECT status FROM orders "
                "WHERE tenant_id=? AND order_no=? AND customer_id=?",
                (tenant, no, cust),
            ).fetchone()
            if order is None:
                return json.dumps({"error": "not_found", "order_no": no},
                                  ensure_ascii=False)
            return json.dumps(
                {"error": "no_shipment", "order_no": no, "order_status": order["status"],
                 "hint": "该订单尚未发货，暂无物流信息"},
                ensure_ascii=False,
            )
        nodes = conn.execute(
            "SELECT * FROM shipment_nodes WHERE tenant_id=? AND order_no=? "
            "ORDER BY node_time",
            (tenant, no),
        ).fetchall()
    return json.dumps(
        {
            "order_no": no,
            "carrier": ship["carrier"],
            "tracking_no": ship["tracking_no"],
            "status": ship["status"],
            "status_name": NODE_NAMES.get(ship["status"], ship["status"]),
            "shipped_at": ship["shipped_at"],
            "estimated_delivery": ship["estimated_delivery"],
            "nodes": [
                {**dict(n), "status_name": NODE_NAMES.get(n["status"], n["status"])}
                for n in nodes
            ],
        },
        ensure_ascii=False,
    )


@tool
def fulfill_atp(sku: str, config: RunnableConfig) -> str:
    """查询商品可承诺库存与时效（skill: fulfill.atp）。sku 如 SKU-A001。"""
    if not ensure_scope(config, "fulfillment.read"):
        return _rejected("fulfillment.read", config)
    tenant = tenant_id(config)
    s = (sku or "").strip().upper()
    if not s:
        return json.dumps({"error": "need_sku"}, ensure_ascii=False)
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM products WHERE tenant_id=? AND sku=?", (tenant, s)
        ).fetchone()
    if row is None:
        return json.dumps({"error": "not_found", "sku": s}, ensure_ascii=False)
    stock = int(row["stock"] or 0)
    on_sale = row["status"] == "on_sale"
    return json.dumps(
        {
            "sku": s,
            "available": stock if on_sale else 0,
            "on_sale": on_sale,
            "ats_days": 1 if stock > 0 and on_sale else 7,
            "hint": (
                "现货，预计 1 个工作日内发出" if stock > 0 and on_sale
                else ("已下架，无法承诺" if not on_sale else "缺货，预计补货 7 天")
            ),
        },
        ensure_ascii=False,
    )


@tool
def return_create(order_no: str, reason: str, config: RunnableConfig) -> str:
    """创建退库单/退货入库单草稿（skill: return.create）。

    Agent 只创建退库单（status=pending_confirm），入库确认由仓库人工完成；
    退款执行走财务审批流——金额实际操作全部人工，Agent 不执行退款。
    biz_key 幂等：同会话同订单重复创建只产生一张退库单。
    """
    if not ensure_scope(config, "fulfillment.write"):
        return _rejected("fulfillment.write", config)
    tenant = tenant_id(config)
    no = (order_no or "").strip().upper()
    reason_text = (reason or "").strip() or "客户退货"
    if not no:
        return json.dumps({"error": "need_order_no"}, ensure_ascii=False)
    try:
        cust = resolve_customer(config)
    except AuthzError as e:
        return e.as_json()
    meta = (config or {}).get("metadata") or {}
    context_id = str(meta.get("context_id") or meta.get("trace_id") or "adhoc")
    biz_key = f"{context_id}:{no}:returndoc"

    with _db() as conn:
        order = conn.execute(
            "SELECT order_no FROM orders WHERE tenant_id=? AND order_no=? "
            "AND customer_id=?",
            (tenant, no, cust),
        ).fetchone()
        if order is None:
            return json.dumps(
                {"error": "not_found", "order_no": no,
                 "hint": "订单不存在或不属于当前客户，不能创建退库单"},
                ensure_ascii=False)
        existing = conn.execute(
            "SELECT * FROM return_orders WHERE tenant_id=? AND biz_key=?",
            (tenant, biz_key),
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO return_orders(tenant_id, return_id, order_no, "
                "customer_id, reason, status, biz_key) "
                "VALUES (?,?,?,?,?,'pending_confirm',?)",
                (tenant, f"RT-{secrets.token_hex(8)}", no, cust,
                 reason_text, biz_key),
            )
            conn.commit()
        row = conn.execute(
            "SELECT * FROM return_orders WHERE tenant_id=? AND biz_key=?",
            (tenant, biz_key),
        ).fetchone()

    audit_from_config(
        config, "tool.return_create", target=f"returndoc:{row['return_id']}",
        params={"order_no": no, "reason": row["reason"], "status": row["status"]},
        result_summary=(f"退库单 {row['return_id']} status={row['status']} "
                        f"idempotent={existing is not None}"),
        context_id=str(meta.get("context_id", "")),
        task_id=str(meta.get("task_id", "")),
    )
    return json.dumps(
        {"return_id": row["return_id"], "order_no": no, "reason": row["reason"],
         "status": row["status"], "biz_key": biz_key,
         "idempotent": existing is not None,
         "hint": "退库单已创建，等待仓库人工确认入库；"
                 "退款执行由财务人工在审批通过后操作（金额操作全部人工）"},
        ensure_ascii=False,
    )


TOOLS = [fulfill_track, fulfill_atp, return_create]
TOOL_TO_SKILL = {
    "fulfill_track": "fulfill.track",
    "fulfill_atp": "fulfill.atp",
    "return_create": "return.create",
}

