"""客户域工具（skill 17-20 + search/overview 升格）。

- customer.profile：客户资料/等级/折扣/客户经理（读）
- credit.query：授信额度、已用/剩余额度、账期天数与到期日（读）
- reconciliation.query：对账单金额/状态/开票状态（读）
- complaint.list：历史投诉记录（读）
- customer.search：坐席按 客户ID/手机号/姓名 检索客户（读，staff-only）
- customer.overview：客户 360 只读聚合（读）

全部经 shared/authz.py：tenant 强制 + customer 归属解析（customer=token.sub；
staff/service 必须显式 customer_id），无身份一律拒绝。
search 为 staff-only：customer 角色 token 一律 not_found（防枚举他人客户）。
"""

import json
import re

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from shared.auth import ensure_scope
from shared.authz import AuthzError, require_customer_exists, resolve_customer
from shared.db import connect_business_db
from shared.pii import mask_obj
from shared.tenant import is_customer, tenant_id


def _rejected(scope: str) -> str:
    return json.dumps({"error": "rejected", "need_scope": scope}, ensure_ascii=False)


def _bind(config: dict | None, requested: str = "") -> tuple[str, str] | str:
    """返回 (tenant, customer)；失败返回错误 JSON 字符串。

    requested 优先于 metadata.customer_id（customer 角色 requested 必须等于
    token.sub，否则 not_found——见 shared/authz.resolve_customer）。
    """
    if not ensure_scope(config, "customer.read"):
        return _rejected("customer.read")
    tenant = tenant_id(config)
    try:
        cust = resolve_customer(config, requested=requested)
    except AuthzError as e:
        return e.as_json()
    with connect_business_db() as conn:
        try:
            require_customer_exists(conn, tenant, cust)
        except AuthzError as e:
            return e.as_json()
    return tenant, cust


def _staff_read(config: dict | None) -> str | None:
    """staff-only 读闸：customer 角色 token 统一 not_found（错误码不区分，
    避免客户角色通过工具枚举同租户其他客户）。失败返回错误 JSON。"""
    if not ensure_scope(config, "customer.read"):
        return _rejected("customer.read")
    if is_customer(config):
        return json.dumps({"error": "not_found", "detail": "object not found"},
                          ensure_ascii=False)
    return None


@tool
def customer_profile(config: RunnableConfig) -> str:
    """查询客户资料（skill: customer.profile）：编码/等级/折扣/客户经理。"""
    bound = _bind(config)
    if isinstance(bound, str):
        return bound
    tenant, cust = bound
    with connect_business_db() as conn:
        row = conn.execute(
            "SELECT c.customer_id, c.name, c.tier, c.phone, c.registered_at, c.city, "
            "p.customer_code, p.dealer_level, p.discount, p.priority, "
            "p.account_manager, p.risk_level "
            "FROM customers c LEFT JOIN customer_profiles p "
            "ON p.tenant_id=c.tenant_id AND p.customer_id=c.customer_id "
            "WHERE c.tenant_id=? AND c.customer_id=?",
            (tenant, cust),
        ).fetchone()
    if row is None:
        return json.dumps({"error": "not_found", "customer_id": cust},
                          ensure_ascii=False)
    data = mask_obj(dict(row))
    return json.dumps(data, ensure_ascii=False)


@tool
def credit_query(config: RunnableConfig) -> str:
    """查询授信与账期（skill: credit.query）：额度/已用/剩余/账期天数/到期日。"""
    bound = _bind(config)
    if isinstance(bound, str):
        return bound
    tenant, cust = bound
    with connect_business_db() as conn:
        row = conn.execute(
            "SELECT credit_limit, credit_used, credit_days, credit_due_date, "
            "remaining_days FROM customer_profiles "
            "WHERE tenant_id=? AND customer_id=?",
            (tenant, cust),
        ).fetchone()
    if row is None:
        return json.dumps({"error": "not_found", "customer_id": cust},
                          ensure_ascii=False)
    d = dict(row)
    limit = float(d.get("credit_limit") or 0)
    used = float(d.get("credit_used") or 0)
    d["credit_remaining"] = round(limit - used, 2)
    return json.dumps(d, ensure_ascii=False)


@tool
def reconciliation_query(config: RunnableConfig, period: str = "") -> str:
    """查询对账单（skill: reconciliation.query）。period 如 2026-10，空则查全部。"""
    bound = _bind(config)
    if isinstance(bound, str):
        return bound
    tenant, cust = bound
    sql = ("SELECT reconciliation_id, period, amount, status, invoice_status, "
           "due_date FROM reconciliations WHERE tenant_id=? AND customer_id=?")
    args: list = [tenant, cust]
    if period.strip():
        sql += " AND period=?"
        args.append(period.strip())
    sql += " ORDER BY period DESC LIMIT 12"
    with connect_business_db() as conn:
        rows = conn.execute(sql, tuple(args)).fetchall()
    if not rows:
        return json.dumps(
            {"error": "no_reconciliation", "customer_id": cust,
             "hint": "该客户名下无对账单"},
            ensure_ascii=False,
        )
    return json.dumps([dict(r) for r in rows], ensure_ascii=False)


@tool
def complaint_list(config: RunnableConfig, limit: str = "5") -> str:
    """查询历史投诉记录（skill: complaint.list）。limit 最多 20。"""
    bound = _bind(config)
    if isinstance(bound, str):
        return bound
    tenant, cust = bound
    try:
        n = max(1, min(int(limit or 5), 20))
    except ValueError:
        n = 5
    with connect_business_db() as conn:
        rows = conn.execute(
            "SELECT complaint_id, order_no, type, content, status, created_at "
            "FROM complaints WHERE tenant_id=? AND customer_id=? "
            "ORDER BY created_at DESC LIMIT ?",
            (tenant, cust, n),
        ).fetchall()
    return json.dumps(
        {"complaints": [dict(r) for r in rows],
         "answer": "已找到投诉记录" if rows else "该客户无投诉记录"},
        ensure_ascii=False,
    )


@tool
def customer_search(config: RunnableConfig, query: str = "") -> str:
    """按 客户ID / 手机号 / 姓名 检索客户（skill: customer.search，staff-only）。

    query 为 CU-1001 / 手机号片段 / 姓名关键词；同租户内模糊匹配，输出脱敏。
    """
    deny = _staff_read(config)
    if deny:
        return deny
    tenant = tenant_id(config)
    q = (query or "").strip()
    if not q:
        return json.dumps({"error": "bad_request", "detail": "query 为空"},
                          ensure_ascii=False)
    if re.fullmatch(r"CU-\d+", q, re.IGNORECASE):
        sql = ("SELECT customer_id, name, tier, phone, registered_at, city "
               "FROM customers WHERE tenant_id=? AND customer_id LIKE ?")
        args = (tenant, f"%{q.upper()}%")
    elif q.isdigit():
        sql = ("SELECT customer_id, name, tier, phone, registered_at, city "
               "FROM customers WHERE tenant_id=? AND phone LIKE ?")
        args = (tenant, f"%{q}%")
    else:
        sql = ("SELECT customer_id, name, tier, phone, registered_at, city "
               "FROM customers WHERE tenant_id=? AND name LIKE ?")
        args = (tenant, f"%{q}%")
    with connect_business_db() as conn:
        rows = conn.execute(sql, args).fetchall()
    return json.dumps(mask_obj([dict(r) for r in rows]), ensure_ascii=False)


@tool
def customer_overview(config: RunnableConfig, customer_id: str = "") -> str:
    """客户 360 只读聚合（skill: customer.overview）：资料/订单/工单/投诉/售后/对账。

    customer 角色=查自己；staff/service 必须显式 customer_id（来自会话绑定）。
    """
    bound = _bind(config, requested=customer_id)
    if isinstance(bound, str):
        return bound
    tenant, cust = bound
    with connect_business_db() as conn:
        customer = conn.execute(
            "SELECT * FROM customers WHERE tenant_id=? AND customer_id=?",
            (tenant, cust),
        ).fetchone()
        if customer is None:
            return json.dumps({"error": "not_found", "customer_id": cust},
                              ensure_ascii=False)
        profile = conn.execute(
            "SELECT * FROM customer_profiles WHERE tenant_id=? AND customer_id=?",
            (tenant, cust),
        ).fetchone()
        orders = conn.execute(
            "SELECT order_no, sku, qty, amount, status, created_at, paid_at "
            "FROM orders WHERE tenant_id=? AND customer_id=? "
            "ORDER BY created_at DESC LIMIT 10",
            (tenant, cust),
        ).fetchall()
        tickets = conn.execute(
            "SELECT ticket_no, sku, fault_type, status, priority, assignee_id, "
            "department, created_at FROM tickets "
            "WHERE tenant_id=? AND customer_id=? ORDER BY created_at DESC LIMIT 10",
            (tenant, cust),
        ).fetchall()
        complaints = conn.execute(
            "SELECT complaint_id, order_no, type, content, status, created_at "
            "FROM complaints WHERE tenant_id=? AND customer_id=? "
            "ORDER BY created_at DESC LIMIT 10",
            (tenant, cust),
        ).fetchall()
        aftersales = conn.execute(
            "SELECT aftersale_id, order_no, sku, buy_date, fault_type, status "
            "FROM aftersales WHERE tenant_id=? AND customer_id=? "
            "ORDER BY buy_date DESC LIMIT 10",
            (tenant, cust),
        ).fetchall()
        reconciliations = conn.execute(
            "SELECT reconciliation_id, period, amount, status, invoice_status, "
            "due_date FROM reconciliations WHERE tenant_id=? AND customer_id=? "
            "ORDER BY period DESC LIMIT 10",
            (tenant, cust),
        ).fetchall()
    return json.dumps({
        "customer": mask_obj(dict(customer)),
        "profile": mask_obj(dict(profile)) if profile else {},
        "orders": [dict(r) for r in orders],
        "tickets": [dict(r) for r in tickets],
        "complaints": [dict(r) for r in complaints],
        "aftersales": [dict(r) for r in aftersales],
        "reconciliations": [dict(r) for r in reconciliations],
    }, ensure_ascii=False)


TOOLS = [customer_profile, credit_query, reconciliation_query, complaint_list,
         customer_search, customer_overview]
TOOL_TO_SKILL = {
    "customer_profile": "customer.profile",
    "credit_query": "credit.query",
    "reconciliation_query": "reconciliation.query",
    "complaint_list": "complaint.list",
    "customer_search": "customer.search",
    "customer_overview": "customer.overview",
}
