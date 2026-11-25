"""securityd：审计/审批/确认单写者（:8309）。

只有它打开 security.db；业务服务通过 shared/security_client.py 调用。
服务间认证：service JWT + aud=securityd + security.read/write scope。
"""

from __future__ import annotations

import os

# 必须在导入 shared.* 之前固定 securityd 的运行模式与库路径。
os.environ["SECURITY_MODE"] = "local"
os.environ["SHARED_DB_PATH"] = os.environ.get(
    "SECURITY_DB_PATH", ".data/security.db"
)

from starlette.applications import Starlette  # noqa: E402
from starlette.responses import JSONResponse  # noqa: E402
from starlette.routing import Route  # noqa: E402

from security_service.store import SecurityStore  # noqa: E402
from shared.auth import AuthError, auth_from_headers  # noqa: E402
from shared.tracing import start_server_span  # noqa: E402

store = SecurityStore()


class TraceSpanMiddleware:
    """每个 HTTP 请求一个 span：父 = 调用方 traceparent 头（security_client 注入）。

    tracing 关闭时 start_server_span 为 no-op，零开销。
    """

    def __init__(self, app) -> None:  # noqa: ANN001
        self._app = app

    async def __call__(self, scope, receive, send) -> None:  # noqa: ANN001
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        headers = {k.decode("latin-1"): v.decode("latin-1")
                   for k, v in scope.get("headers", [])}
        with start_server_span(
            f"securityd {scope['method']} {scope['path']}",
            parent_traceparent=headers.get("traceparent", ""),
        ):
            await self._app(scope, receive, send)


def _guard(request, *, write: bool):  # noqa: ANN001, ANN202
    try:
        auth = auth_from_headers(
            dict(request.headers), expected_audience="securityd",
        )
    except AuthError:
        auth = None
    if auth is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if auth.role != "service":
        return JSONResponse({"error": "forbidden", "detail": "service token required"},
                            status_code=403)
    scope = "security.write" if write else "security.read"
    if not auth.has_scope(scope):
        return JSONResponse({"error": "forbidden", "need_scope": scope},
                            status_code=403)
    return None


async def health(request):  # noqa: ANN001, ANN201
    return JSONResponse({"ok": True, "service": "securityd"})


async def audit_append(request):  # noqa: ANN001, ANN201
    denied = _guard(request, write=True)
    if denied:
        return denied
    payload = await request.json()
    event_id = store.audit_append(payload)
    return JSONResponse({"id": event_id})


async def audit_list(request):  # noqa: ANN001, ANN201
    denied = _guard(request, write=False)
    if denied:
        return denied
    q = request.query_params
    tenant = q.get("tenant_id", "")
    events = store.audit_list(
        tenant_id=tenant or None,
        limit=int(q.get("limit", "100")),
        trace_id=q.get("trace_id", ""),
        context_id=q.get("context_id", ""),
        action=q.get("action", ""),
    )
    return JSONResponse({"events": events})


async def audit_verify(request):  # noqa: ANN001, ANN201
    denied = _guard(request, write=False)
    if denied:
        return denied
    tenant = request.query_params.get("tenant_id") or None
    return JSONResponse(store.audit_verify(tenant))


async def approval_create(request):  # noqa: ANN001, ANN201
    denied = _guard(request, write=True)
    if denied:
        return denied
    return JSONResponse(store.approval_create(await request.json()))


async def approval_list(request):  # noqa: ANN001, ANN201
    denied = _guard(request, write=False)
    if denied:
        return denied
    q = request.query_params
    return JSONResponse({"approvals": store.approval_list(
        q.get("tenant_id", ""), q.get("status") or None,
        int(q.get("limit", "50")),
    )})


async def approval_find(request):  # noqa: ANN001, ANN201
    denied = _guard(request, write=False)
    if denied:
        return denied
    q = request.query_params
    amount_raw = q.get("amount")
    found = store.approval_find(
        tenant_id=q.get("tenant_id", ""), action=q.get("action", ""),
        target=q.get("target", ""),
        amount=float(amount_raw) if amount_raw not in (None, "") else None,
        requested_by=q.get("requested_by", ""),
    )
    return JSONResponse(found or {})


async def approval_get(request):  # noqa: ANN001, ANN201
    denied = _guard(request, write=False)
    if denied:
        return denied
    found = store.approval_get(
        request.query_params.get("tenant_id", ""), request.path_params["aid"],
    )
    if not found:
        return JSONResponse({"error": "not_found"}, status_code=404)
    return JSONResponse(found)


async def approval_approve(request):  # noqa: ANN001, ANN201
    denied = _guard(request, write=True)
    if denied:
        return denied
    body = await request.json()
    return JSONResponse(store.approval_approve(
        body.get("tenant_id", ""), request.path_params["aid"],
        actor_id=body.get("actor_id", ""), role=body.get("role", ""),
    ))


async def approval_reject(request):  # noqa: ANN001, ANN201
    denied = _guard(request, write=True)
    if denied:
        return denied
    body = await request.json()
    return JSONResponse(store.approval_reject(
        body.get("tenant_id", ""), request.path_params["aid"],
        actor_id=body.get("actor_id", ""), role=body.get("role", ""),
        reason=body.get("reason", ""),
    ))


async def approval_supersede(request):  # noqa: ANN001, ANN201
    denied = _guard(request, write=True)
    if denied:
        return denied
    body = await request.json()
    closed = store.approval_supersede(body)
    return JSONResponse({"closed": closed})


async def approval_consume(request):  # noqa: ANN001, ANN201
    denied = _guard(request, write=True)
    if denied:
        return denied
    body = await request.json()
    return JSONResponse(store.approval_consume(
        body.get("tenant_id", ""), request.path_params["aid"],
        action=body.get("action", ""), target=body.get("target", ""),
        amount=body.get("amount"), actor_id=body.get("actor_id", ""),
        role=body.get("role", ""),
    ))


async def confirmation_get(request):  # noqa: ANN001, ANN201
    denied = _guard(request, write=False)
    if denied:
        return denied
    found = store.confirmation_get(
        request.path_params["token"], request.query_params.get("tenant_id", ""),
    )
    if not found:
        return JSONResponse({"error": "not_found"}, status_code=404)
    return JSONResponse(found)


async def confirmation_create(request):  # noqa: ANN001, ANN201
    denied = _guard(request, write=True)
    if denied:
        return denied
    return JSONResponse(store.confirmation_create(await request.json()))


async def confirmation_consume(request):  # noqa: ANN001, ANN201
    denied = _guard(request, write=True)
    if denied:
        return denied
    return JSONResponse(store.confirmation_consume(
        request.path_params["token"], await request.json(),
    ))


routes = [
    Route("/health", health),
    Route("/internal/audit/events", audit_append, methods=["POST"]),
    Route("/internal/audit/events", audit_list, methods=["GET"]),
    Route("/internal/audit/verify", audit_verify, methods=["GET"]),
    Route("/internal/approvals", approval_create, methods=["POST"]),
    Route("/internal/approvals", approval_list, methods=["GET"]),
    Route("/internal/approvals/find", approval_find, methods=["GET"]),
    Route("/internal/approvals/{aid}", approval_get, methods=["GET"]),
    Route("/internal/approvals/{aid}/approve", approval_approve, methods=["POST"]),
    Route("/internal/approvals/{aid}/reject", approval_reject, methods=["POST"]),
    Route("/internal/approvals/{aid}/consume", approval_consume, methods=["POST"]),
    Route("/internal/approvals/supersede", approval_supersede, methods=["POST"]),
    Route("/internal/confirmations", confirmation_create, methods=["POST"]),
    Route("/internal/confirmations/{token}", confirmation_get, methods=["GET"]),
    Route("/internal/confirmations/{token}/consume", confirmation_consume,
          methods=["POST"]),
]

app = Starlette(routes=routes)
app.add_middleware(TraceSpanMiddleware)
