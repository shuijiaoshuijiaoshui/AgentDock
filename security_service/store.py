"""securityd 的本地存储门面：审计/审批/确认单写者。

securityd 进程只以 local 模式运行；业务服务以 remote 模式经 SecurityClient 调用。
"""

from __future__ import annotations

from shared import approvals, audit, confirmations


class SecurityStore:
    # ---- audit ----
    def audit_append(self, payload: dict) -> int | None:
        import json

        data = dict(payload)
        created_at = data.pop("created_at", "")
        if isinstance(data.get("params"), str):
            try:
                data["params"] = json.loads(data["params"])
            except json.JSONDecodeError:
                data["params"] = {}
        return audit.audit_event(**data, created_at=created_at)

    def audit_list(self, **kwargs) -> list[dict]:
        return audit.list_events(**kwargs)

    def audit_verify(self, tenant_id: str | None = None) -> dict:
        return audit.verify_chain(tenant_id)

    # ---- approvals ----
    def approval_create(self, payload: dict) -> dict:
        data = dict(payload)
        data["approver_roles"] = tuple(data.get("approver_roles") or ())
        data["resume"] = data.get("resume") or {}
        return approvals.create_approval(**data)

    def approval_get(self, tenant_id: str, approval_id: str) -> dict | None:
        return approvals.get_approval(tenant_id, approval_id)

    def approval_list(self, tenant_id: str, status: str | None,
                      limit: int = 50) -> list[dict]:
        return approvals.list_approvals(tenant_id, status, limit)

    def approval_find(self, **kwargs) -> dict | None:
        return approvals.find_approved(**kwargs)

    def approval_approve(self, tenant_id: str, approval_id: str, *,
                         actor_id: str, role: str) -> dict:
        return approvals.approve(tenant_id, approval_id, actor_id=actor_id, role=role)

    def approval_reject(self, tenant_id: str, approval_id: str, *,
                        actor_id: str, role: str, reason: str = "") -> dict:
        return approvals.reject(
            tenant_id, approval_id, actor_id=actor_id, role=role, reason=reason,
        )

    def approval_supersede(self, payload: dict) -> int:
        data = dict(payload)
        return approvals.supersede_siblings(
            data.get("tenant_id", ""), action=data.get("action", ""),
            target=data.get("target", ""),
            except_id=data.get("except_id") or None,
            reason=data.get("reason", ""),
            actor_id=data.get("actor_id", "system"),
            role=data.get("role", "service"),
        )

    def approval_consume(self, tenant_id: str, approval_id: str, *,
                         action: str, target: str, amount: float | None,
                         actor_id: str, role: str) -> dict:
        return approvals.consume(
            tenant_id, approval_id, action=action, target=target,
            amount=amount, actor_id=actor_id, role=role,
        )

    # ---- confirmations ----
    def confirmation_get(self, token: str, tenant_id: str) -> dict | None:
        return confirmations.get_confirmation(token, tenant_id=tenant_id)

    def confirmation_create(self, payload: dict) -> dict:
        return confirmations.create_confirmation(**payload)

    def confirmation_consume(self, token: str, payload: dict) -> dict:
        return confirmations.consume_confirmation(token, **payload)
