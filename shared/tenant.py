"""租户/客户上下文提取（工具与编排共用）。

- tenant/actor 只从验签 token claim 取；无身份一律 fail-closed，不再回落 t_demo；
- customer：customer 角色以 token.sub 为准；坐席/主管必须显式携带 metadata.customer_id
  （同租户内的行级操作对象），域查询仍强制 tenant 过滤。
"""

from __future__ import annotations

from shared.auth import AuthError, auth_from_config


def tenant_id(config: dict | None) -> str:
    auth = auth_from_config(config)
    if auth is None or not auth.tenant_id:
        raise AuthError("missing verified tenant")
    return auth.tenant_id


def actor_id(config: dict | None) -> str:
    auth = auth_from_config(config)
    if auth is None or not auth.actor_id:
        raise AuthError("missing verified actor")
    return auth.actor_id


def role(config: dict | None) -> str:
    auth = auth_from_config(config)
    if auth is None or not auth.role:
        raise AuthError("missing verified role")
    return auth.role


def customer_id(config: dict | None) -> str:
    auth = auth_from_config(config)
    if auth is None:
        raise AuthError("missing verified auth")
    if auth.role == "customer":
        return auth.actor_id
    meta = (config or {}).get("metadata") or {}
    if auth.role == "service" and not meta.get("customer_id"):
        return auth.on_behalf_of
    return str(meta.get("customer_id") or "")


def is_customer(config: dict | None) -> bool:
    auth = auth_from_config(config)
    return bool(auth and auth.role == "customer")
