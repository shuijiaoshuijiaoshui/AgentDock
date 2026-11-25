"""统一对象级授权（tenant + customer + object ownership）。

规则（1A 安全基线）：
- customer 角色：对象必须属于 token.sub；请求里带别的 customer_id 直接拒绝；
- agent/supervisor/finance/admin：必须显式携带 metadata.customer_id（来自会话/工单），
  且该客户必须存在同租户；
- service：必须带 on_behalf_of 或 metadata.customer_id，不允许“无客户全量”；
- 归属不符统一返回 not_found，避免通过错误码枚举他人订单号/工单号。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from shared.auth import AuthError, auth_from_config


class AuthzError(Exception):
    """可安全返回给调用方的授权/归属错误。"""

    def __init__(self, code: str, *, detail: str = "", status: int = 404) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail
        self.status = status

    def as_json(self) -> str:
        payload = {"error": self.code}
        if self.detail:
            payload["detail"] = self.detail
        return json.dumps(payload, ensure_ascii=False)


def resolve_customer(config: dict | None, requested: str = "") -> str:
    """解析本次操作绑定的 customer_id；无合法绑定则抛 AuthzError。"""
    auth = auth_from_config(config)
    if auth is None:
        raise AuthzError("unauthorized", detail="missing verified auth", status=401)
    req = (requested or "").strip()
    if auth.role == "customer":
        if req and req != auth.actor_id:
            # 不告诉调用方“对象存在但不属于你”
            raise AuthzError("not_found", detail="object not found")
        return auth.actor_id

    meta = (config or {}).get("metadata") or {}
    cust = req or str(meta.get("customer_id") or "").strip()
    if auth.role == "service":
        obo = (auth.on_behalf_of or "").strip()
        if obo:
            # service token 已绑定客户：requested 必须为空或等于 on_behalf_of
            if cust and cust != obo:
                raise AuthzError(
                    "not_found",
                    detail="service token is bound to another customer",
                )
            return obo
        if not cust:
            raise AuthzError(
                "customer_id_required",
                detail="service token has no on_behalf_of and no customer_id",
                status=400,
            )
        return cust
    if not cust:
        raise AuthzError(
            "customer_id_required",
            detail="staff/service must pass an explicit customer_id",
            status=400,
        )
    return cust


def require_customer_exists(
    conn: sqlite3.Connection, tenant_id: str, customer_id: str
) -> None:
    row = conn.execute(
        "SELECT 1 FROM customers WHERE tenant_id=? AND customer_id=?",
        (tenant_id, customer_id),
    ).fetchone()
    if row is None:
        raise AuthzError("not_found", detail="customer not found")


def find_order(
    conn: sqlite3.Connection, tenant_id: str, order_no: str, customer_id: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM orders WHERE tenant_id=? AND order_no=? AND customer_id=?",
        (tenant_id, order_no.strip().upper(), customer_id),
    ).fetchone()


def require_order(
    conn: sqlite3.Connection, tenant_id: str, order_no: str, customer_id: str
) -> sqlite3.Row:
    row = find_order(conn, tenant_id, order_no, customer_id)
    if row is None:
        raise AuthzError("not_found", detail="order not found")
    return row


def require_scope(config: dict | None, scope: str) -> None:
    """scope 不足时抛 AuthzError（工具层统一转 JSON）。"""
    auth = auth_from_config(config)
    if auth is None:
        raise AuthzError("unauthorized", detail="missing verified auth", status=401)
    if not auth.has_scope(scope):
        raise AuthzError("rejected", detail=f"need_scope={scope}", status=403)


def ensure_owned_row(row: Any, customer_id: str) -> Any:
    """通用防御：数据库行若带 customer_id，必须与解析出的客户一致。"""
    if row is None:
        raise AuthzError("not_found", detail="object not found")
    if "customer_id" in row.keys() and str(row["customer_id"]) != customer_id:
        raise AuthzError("not_found", detail="object not found")
    return row


__all__ = [
    "AuthError",
    "AuthzError",
    "ensure_owned_row",
    "find_order",
    "require_customer_exists",
    "require_order",
    "require_scope",
    "resolve_customer",
]
