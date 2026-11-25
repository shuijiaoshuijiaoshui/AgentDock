"""售后域工具（技术方案第 6 章 skill 9-12）。

- aftersale.eligibility：退换修资格判定——内部嵌 PolicyClient 查保修条款（不建
  Task，PRD 7.3.7 政策例外），Artifact 引用 clause_id+version+生效期（AC-31）
- aftersale.reason：责任判定（规则：质量/人为/物流）
- ticket.create：建维修工单（写操作；补偿=作废工单，Saga M3 接）
- ticket.query：工单进度查询
"""

import json
import secrets
import sqlite3
from datetime import date
from pathlib import Path

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from shared.db import business_db_path
from shared.auth import auth_from_config, ensure_scope
from shared.audit import audit_from_config
from shared.authz import AuthzError, require_customer_exists, resolve_customer
from shared.confirmations import consume_confirmation, create_confirmation, get_confirmation
from shared.policy_client import PolicyUnavailable, cite, lookup_clauses
from shared.tenant import tenant_id




def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(business_db_path())
    conn.row_factory = sqlite3.Row
    return conn


def _rejected(scope: str) -> str:
    return json.dumps({"error": "rejected", "need_scope": scope}, ensure_ascii=False)


@tool
def judge_eligibility(
    sku: str, config: RunnableConfig, order_no: str = "", buy_date: str = ""
) -> str:
    """判定退换修资格（skill: aftersale.eligibility）。

    购买日期只从 orders（paid_at）或 aftersales（customer_id + sku）推导；
    用户自报 buy_date 只作为参考返回，绝不参与判定。
    """
    if not ensure_scope(config, "aftersale.read"):
        return _rejected("aftersale.read")
    tenant = tenant_id(config)
    try:
        cust = resolve_customer(config)
    except AuthzError as e:
        return e.as_json()
    s = (sku or "").strip().upper()
    no = (order_no or "").strip().upper()
    user_buy = (buy_date or "").strip()
    if not s:
        return json.dumps({"error": "need_sku"}, ensure_ascii=False)

    with _db() as conn:
        try:
            require_customer_exists(conn, tenant, cust)
        except AuthzError as e:
            return e.as_json()
        buy = ""
        source = ""
        if no:
            order = conn.execute(
                "SELECT order_no, sku, paid_at FROM orders "
                "WHERE tenant_id=? AND order_no=? AND customer_id=?",
                (tenant, no, cust),
            ).fetchone()
            if order is None:
                return json.dumps(
                    {"error": "not_found", "order_no": no}, ensure_ascii=False)
            if s and order["sku"] and str(order["sku"]).upper() != s:
                return json.dumps(
                    {"error": "sku_mismatch", "order_no": no,
                     "order_sku": order["sku"], "sku": s},
                    ensure_ascii=False)
            if not order["paid_at"]:
                return json.dumps(
                    {"error": "unpaid_order", "order_no": no,
                     "hint": "订单未支付，不能作为购买记录"},
                    ensure_ascii=False)
            buy, source = str(order["paid_at"]), "orders.paid_at"
        else:
            order = conn.execute(
                "SELECT order_no, sku, paid_at FROM orders "
                "WHERE tenant_id=? AND customer_id=? AND sku=? "
                "AND paid_at IS NOT NULL AND paid_at != '' "
                "ORDER BY paid_at DESC LIMIT 1",
                (tenant, cust, s),
            ).fetchone()
            if order is not None:
                no, buy, source = str(order["order_no"]), str(order["paid_at"]), "orders.paid_at"
            else:
                af = conn.execute(
                    "SELECT order_no, buy_date FROM aftersales "
                    "WHERE tenant_id=? AND customer_id=? AND sku=? "
                    "ORDER BY buy_date DESC LIMIT 1",
                    (tenant, cust, s),
                ).fetchone()
                if af is not None:
                    no, buy, source = (
                        str(af["order_no"] or ""), str(af["buy_date"] or ""),
                        "aftersales.buy_date",
                    )
        if not buy:
            return json.dumps(
                {"error": "no_purchase_record", "sku": s, "customer_id": cust,
                 "hint": "该客户无此 SKU 的购买记录，无法判定资格"},
                ensure_ascii=False)

    try:
        bought_on = date.fromisoformat(buy[:10])
    except ValueError:
        return json.dumps(
            {"error": "invalid_purchase_date", "buy_date": buy},
            ensure_ascii=False)
    days = (date.today() - bought_on).days

    # 条款依据来自政策域内部 API（不建 Task）；不可用时降级、明确标注
    try:
        clause = lookup_clauses(
            query="保修", auth=auth_from_config(config)
        )[0]
        basis, degraded = cite(clause), False
    except (PolicyUnavailable, IndexError):
        clause, basis, degraded = None, "条款暂不可查（政策服务不可用），仅按 12 个月估算", True

    eligible = days <= 365
    out = {
        "sku": s, "order_no": no, "customer_id": cust,
        "buy_date": buy, "buy_date_source": source,
        "days_since_purchase": days,
        "eligible": eligible,
        "conclusion": "保修内" if eligible else "保修外（超 12 个月）",
        "clause": basis, "degraded": degraded,
    }
    if user_buy and user_buy != buy:
        out["buy_date_unverified"] = user_buy
        out["ignored_user_buy_date"] = True
    return json.dumps(out, ensure_ascii=False)


@tool
def determine_responsibility(fault_type: str, config: RunnableConfig) -> str:
    """责任判定（skill: aftersale.reason）。fault_type 如：按键失灵/进水/屏幕闪烁/物流损坏。"""
    if not ensure_scope(config, "aftersale.read"):
        return _rejected("aftersale.read")
    f = fault_type or ""
    tenant = tenant_id(config)
    if any(w in f for w in ("进水", "摔", "人为", "私拆")):
        resp, detail = "buyer", "人为损坏，不在保修范围"
    elif any(w in f for w in ("物流", "运输", "破损签收")):
        resp, detail = "carrier", "物流责任，运费与维修由物流方承担"
    else:
        resp, detail = "seller", "质量问题，商家承担维修与运费"
    # 运费承担依据条款（能取到就引用，取不到不编造）
    try:
        clause = lookup_clauses(
            query="运费", auth=auth_from_config(config)
        )[0]
        basis = cite(clause)
    except (PolicyUnavailable, IndexError):
        basis = "条款暂不可查"
    return json.dumps(
        {"fault_type": f, "responsibility": resp, "detail": detail,
         "freight_clause": basis},
        ensure_ascii=False)


@tool
def create_ticket(sku: str, fault_type: str, config: RunnableConfig,
                  customer_id: str = "", confirm_token: str = "") -> str:
    """创建维修工单（skill: ticket.create，写操作）。

    安全机制：tenant 过滤 + 两阶段确认 + 幂等 + 执行后回读校验。
    """
    if not ensure_scope(config, "aftersale.write"):
        return _rejected("aftersale.write")
    tenant = tenant_id(config)
    try:
        cust = resolve_customer(config, customer_id)
    except AuthzError as e:
        return e.as_json()
    meta = (config or {}).get("metadata") or {}
    context_id = meta.get("context_id") or "adhoc"
    biz_key = f"{context_id}:{sku}:{fault_type}"
    with _db() as conn:
        try:
            require_customer_exists(conn, tenant, cust)
        except AuthzError as e:
            return e.as_json()
    audit_from_config(
        config, "tool.ticket_create", target=f"ticket:{sku}",
        params={"sku": sku, "fault_type": fault_type, "customer_id": cust},
        context_id=str(meta.get("context_id", "")),
        task_id=str(meta.get("task_id", "")),
    )

    # 幂等重放：同会话同 SKU/故障已建单 → 直接返回原单，不再消耗确认单
    with _db() as conn:
        existing = conn.execute(
            "SELECT ticket_no, status, sku, fault_type FROM tickets "
            "WHERE tenant_id=? AND biz_key=?",
            (tenant, biz_key),
        ).fetchone()
    if existing is not None:
        if confirm_token:
            conf = get_confirmation(confirm_token, tenant_id=tenant)
            valid_scope = bool(
                conf
                and conf.get("action") == "ticket.create"
                and conf.get("target") == f"{sku}:{fault_type}"
                and (conf.get("customer_id") or "") == (cust or "")
                and conf.get("actor_id") == auth_from_config(config).actor_id
            )
            if not valid_scope or conf.get("status") == "expired":
                return json.dumps(
                    {"error": "invalid_confirm_token", "detail": "confirmation_scope_mismatch"},
                    ensure_ascii=False)
            if conf.get("status") == "pending":
                consumed = consume_confirmation(
                    confirm_token, tenant_id=tenant,
                    actor_id=auth_from_config(config).actor_id,
                    customer_id=cust, action="ticket.create",
                    target=f"{sku}:{fault_type}",
                    payload={"sku": sku, "fault_type": fault_type, "customer_id": cust},
                )
                if not consumed.get("ok"):
                    return json.dumps(
                        {"error": "invalid_confirm_token", "detail": consumed.get("error")},
                        ensure_ascii=False)
        verified = existing["sku"] == sku and existing["fault_type"] == fault_type
        audit_from_config(
            config, "tool.ticket_create.result",
            target=f"ticket:{existing['ticket_no']}",
            result_summary=f"inserted=False verified={verified}",
            context_id=str(meta.get("context_id", "")),
        )
        return json.dumps(
            {"ticket_no": existing["ticket_no"], "sku": existing["sku"],
             "fault_type": existing["fault_type"], "status": existing["status"],
             "biz_key": biz_key, "created": False, "idempotent": True,
             "verified": verified,
             "compensation": "作废工单（幂等键即 biz_key，M3 Saga）"},
            ensure_ascii=False)

    confirm_payload = {"sku": sku, "fault_type": fault_type, "customer_id": cust}
    if not confirm_token:
        confirmation = create_confirmation(
            tenant_id=tenant,
            actor_id=auth_from_config(config).actor_id,
            customer_id=cust,
            action="ticket.create",
            target=f"{sku}:{fault_type}",
            payload=confirm_payload,
            context_id=str(meta.get("context_id", "")),
            task_id=str(meta.get("task_id", "")),
        )
        return json.dumps(
            {"status": "pending_confirm", "biz_key": biz_key,
             "preview": {"sku": sku, "fault_type": fault_type,
                         "customer_id": cust or "（未登记）"},
             "confirm_token": confirmation["confirm_token"],
             "expires_at": confirmation["expires_at"],
             "hint": "确认后带 confirm_token 重新调用才会真正建单"},
            ensure_ascii=False)
    confirmed = consume_confirmation(
        confirm_token,
        tenant_id=tenant,
        actor_id=auth_from_config(config).actor_id,
        customer_id=cust,
        action="ticket.create",
        target=f"{sku}:{fault_type}",
        payload=confirm_payload,
    )
    if not confirmed.get("ok"):
        return json.dumps(
            {"error": "invalid_confirm_token", "detail": confirmed.get("error")},
            ensure_ascii=False)

    with _db() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO tickets"
            "(tenant_id, ticket_no, customer_id, sku, fault_type, status, order_no, biz_key) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (tenant, f"TMP-{secrets.token_hex(8)}", cust, sku, fault_type,
             "created", None, biz_key),
        )
        inserted = cur.rowcount == 1
        if inserted:
            rowid = conn.execute(
                "SELECT rowid AS rid FROM tickets WHERE tenant_id=? AND biz_key=?",
                (tenant, biz_key),
            ).fetchone()["rid"]
            ticket_no = f"TK-{4000 + int(rowid)}"
            conn.execute(
                "UPDATE tickets SET ticket_no=? WHERE tenant_id=? AND biz_key=?",
                (ticket_no, tenant, biz_key),
            )
        row = conn.execute(
            "SELECT ticket_no, status, sku, fault_type FROM tickets "
            "WHERE tenant_id=? AND biz_key=?",
            (tenant, biz_key),
        ).fetchone()

    if row is None:
        return json.dumps({"error": "write_failed", "biz_key": biz_key},
                          ensure_ascii=False)
    verified = row["sku"] == sku and row["fault_type"] == fault_type
    audit_from_config(
        config, "tool.ticket_create.result", target=f"ticket:{row['ticket_no']}",
        result_summary=f"inserted={inserted} verified={verified}",
        context_id=str(meta.get("context_id", "")),
    )
    return json.dumps(
        {"ticket_no": row["ticket_no"], "sku": row["sku"],
         "fault_type": row["fault_type"], "status": row["status"],
         "biz_key": biz_key, "created": inserted,
         "idempotent": not inserted, "verified": verified,
         "compensation": "作废工单（幂等键即 biz_key，M3 Saga）"},
        ensure_ascii=False)


@tool
def query_ticket(ticket_no: str, config: RunnableConfig) -> str:
    """查询工单进度（skill: ticket.query）。如 TK-3001。"""
    if not ensure_scope(config, "aftersale.read"):
        return _rejected("aftersale.read")
    tenant = tenant_id(config)
    try:
        cust = resolve_customer(config)
    except AuthzError as e:
        return e.as_json()
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM tickets WHERE tenant_id=? AND ticket_no=? AND customer_id=?",
            (tenant, ticket_no.strip().upper(), cust),
        ).fetchone()
    if row is None:
        return json.dumps({"error": "not_found", "ticket_no": ticket_no},
                          ensure_ascii=False)
    return json.dumps(dict(row), ensure_ascii=False)


TOOLS = [judge_eligibility, determine_responsibility, create_ticket, query_ticket]
TOOL_TO_SKILL = {
    "judge_eligibility": "aftersale.eligibility",
    "determine_responsibility": "aftersale.reason",
    "create_ticket": "ticket.create",
    "query_ticket": "ticket.query",
}
