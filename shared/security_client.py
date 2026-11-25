"""securityd 的同步 HTTP 客户端（审计/审批/确认单写者）。

只有 `SECURITY_MODE=remote` 时才被调用；local 模式由 shared/audit.py、
shared/approvals.py、shared/confirmations.py 直接走单进程 SQLite。
服务间认证：短时 service JWT，aud=securityd，scope=security.read/write。
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from shared.auth import create_token
from shared.config import get_settings
from shared.tracing import inject_trace_headers

logger = logging.getLogger(__name__)


class SecurityUnavailable(RuntimeError):
    """securityd 不可达/拒绝请求；strict 动作必须失败关闭。"""


class SecurityNotFound(SecurityUnavailable):
    """securityd 返回 404（审批/确认单不存在）。"""


def _service_name() -> str:
    raw = os.environ.get("SERVICE_NAME", "") or os.environ.get("SERVICE_TARGET", "")
    if not raw:
        return "security-client"
    raw = raw.split(":", 1)[0]
    parts = [p for p in raw.split(".") if p]
    if len(parts) >= 2 and parts[-1] == "server":
        return parts[-2]
    return parts[-1] if parts else "security-client"


class SecurityClient:
    def __init__(self) -> None:
        # 进程内复用连接；securityd 是本地/内网短连接，避免每次 strict 审计重新握手。
        self._http = httpx.Client(timeout=3.0, trust_env=False)
        self._tokens: dict[str, tuple[str, float]] = {}

    def _base(self) -> str:
        s = get_settings()
        return (s.security_url or f"http://127.0.0.1:{s.security_port}").rstrip("/")

    def _service_token(self, scope: str) -> str:
        # 短时 token；securityd 侧校验 aud/role/scope。按 scope 分别缓存 4 分钟。
        import time

        now = time.time()
        cached = self._tokens.get(scope)
        if cached and now - cached[1] < 240:
            return cached[0]
        token = create_token(
            tenant_id="security",
            actor_id=_service_name(),
            role="service",
            scopes=[scope],
            ttl_seconds=300,
            audience="securityd",
            token_type="service",
        )
        self._tokens[scope] = (token, now)
        return token

    def _request(
        self, method: str, path: str, *, scope: str,
        payload: dict | None = None, params: dict | None = None,
        retries: int = 2,
    ) -> Any:
        last: Exception | None = None
        for attempt in range(retries + 1):
            try:
                resp = self._http.request(
                    method, self._base() + path, json=payload, params=params,
                    headers=inject_trace_headers(
                        {"Authorization": f"Bearer {self._service_token(scope)}"}
                    ),
                )
                if resp.status_code == 404:
                    raise SecurityNotFound(
                        f"securityd {method} {path} -> 404: {resp.text[:200]}"
                    )
                if resp.status_code >= 400:
                    raise SecurityUnavailable(
                        f"securityd {method} {path} -> {resp.status_code}: "
                        f"{resp.text[:200]}"
                    )
                return resp.json() if resp.content else {}
            except (httpx.TransportError, httpx.TimeoutException, OSError) as exc:
                last = exc
                if attempt < retries:
                    continue
            except SecurityUnavailable:
                raise
        raise SecurityUnavailable(f"securityd unreachable: {last}")

    # ---- audit ----
    def audit_append(self, event: dict) -> int | None:
        out = self._request(
            "POST", "/internal/audit/events", scope="security.write", payload=event,
        )
        return out.get("id")

    def audit_list(self, *, tenant_id: str, limit: int = 100,
                   trace_id: str = "", context_id: str = "",
                   action: str = "") -> list[dict]:
        out = self._request(
            "GET", "/internal/audit/events", scope="security.read",
            params={
                "tenant_id": tenant_id, "limit": limit,
                "trace_id": trace_id, "context_id": context_id, "action": action,
            },
        )
        return out.get("events", [])

    def audit_verify(self, tenant_id: str | None = None) -> dict:
        return self._request(
            "GET", "/internal/audit/verify", scope="security.read",
            params={"tenant_id": tenant_id} if tenant_id else None,
        )

    # ---- approvals ----
    def approval_create(self, payload: dict) -> dict:
        return self._request(
            "POST", "/internal/approvals", scope="security.write", payload=payload,
        )

    def approval_get(self, tenant_id: str, approval_id: str) -> dict | None:
        try:
            return self._request(
                "GET", f"/internal/approvals/{approval_id}", scope="security.read",
                params={"tenant_id": tenant_id},
            )
        except SecurityNotFound:
            return None

    def approval_list(self, tenant_id: str, status: str | None,
                      limit: int = 50) -> list[dict]:
        params = {"tenant_id": tenant_id, "limit": limit}
        if status:
            params["status"] = status
        out = self._request("GET", "/internal/approvals", scope="security.read",
                            params=params)
        return out.get("approvals", [])

    def approval_find(self, *, tenant_id: str, action: str, target: str,
                      amount: float | None, requested_by: str) -> dict | None:
        return self._request(
            "GET", "/internal/approvals/find", scope="security.read",
            params={
                "tenant_id": tenant_id, "action": action, "target": target,
                "requested_by": requested_by,
                **({"amount": amount} if amount is not None else {}),
            },
        )

    def approval_approve(self, tenant_id: str, approval_id: str, *,
                         actor_id: str, role: str) -> dict:
        return self._request(
            "POST", f"/internal/approvals/{approval_id}/approve",
            scope="security.write",
            payload={"tenant_id": tenant_id, "actor_id": actor_id, "role": role},
        )

    def approval_reject(self, tenant_id: str, approval_id: str, *,
                        actor_id: str, role: str, reason: str = "") -> dict:
        return self._request(
            "POST", f"/internal/approvals/{approval_id}/reject",
            scope="security.write",
            payload={"tenant_id": tenant_id, "actor_id": actor_id, "role": role,
                     "reason": reason},
        )

    def approval_supersede(self, tenant_id: str, *, action: str, target: str,
                           except_id: str | None = None, reason: str = "",
                           actor_id: str = "system", role: str = "service") -> int:
        out = self._request(
            "POST", "/internal/approvals/supersede", scope="security.write",
            payload={
                "tenant_id": tenant_id, "action": action, "target": target,
                "except_id": except_id or "", "reason": reason,
                "actor_id": actor_id, "role": role,
            },
        )
        return int(out.get("closed") or 0)

    def approval_consume(self, tenant_id: str, approval_id: str, *,
                         action: str, target: str, amount: float | None,
                         actor_id: str, role: str) -> dict:
        return self._request(
            "POST", f"/internal/approvals/{approval_id}/consume",
            scope="security.write",
            payload={
                "tenant_id": tenant_id, "action": action, "target": target,
                "amount": amount, "actor_id": actor_id, "role": role,
            },
        )

    # ---- confirmations ----
    def confirmation_create(self, payload: dict) -> dict:
        return self._request(
            "POST", "/internal/confirmations", scope="security.write",
            payload=payload,
        )

    def confirmation_get(self, token: str, tenant_id: str) -> dict | None:
        try:
            return self._request(
                "GET", f"/internal/confirmations/{token}", scope="security.read",
                params={"tenant_id": tenant_id},
            )
        except SecurityNotFound:
            return None

    def confirmation_consume(self, token: str, payload: dict) -> dict:
        return self._request(
            "POST", f"/internal/confirmations/{token}/consume",
            scope="security.write", payload=payload,
        )


_client: SecurityClient | None = None


def get_client() -> SecurityClient:
    global _client
    if _client is None:
        _client = SecurityClient()
    return _client


def security_mode() -> str:
    return get_settings().security_mode
