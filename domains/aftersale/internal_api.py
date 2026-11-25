"""售后域内部 API（坐席工单 CRUD，双形态之二）。

/internal/* 与 A2A Server 同进程：工单的增删改查逻辑单份实现在域内，
console 只是薄代理（不再直连业务库 SQL）。鉴权走全局 JWT 中间件
（Authorization: Bearer <token>），tenant 从 token claim 取。

挂载点：server.py 把 routes 并进 app（同政策域 internal_api 模式）。
"""

from secrets import token_hex

from starlette.responses import JSONResponse
from starlette.routing import Route

from shared.auth import auth_from_request
from shared.db import connect_business_db

TICKET_DEPARTMENTS = ("warehouse", "finance")
TICKET_STATUSES = ("created", "received", "repairing", "resolved", "closed")


def _can_read(auth) -> bool:  # noqa: ANN001
    return bool(auth) and (auth.has_scope("aftersale.read") or auth.role == "finance")


def _can_write(auth) -> bool:  # noqa: ANN001
    return bool(auth) and (auth.has_scope("aftersale.write") or auth.role == "finance")


def _gen_ticket_no(conn, tenant: str) -> str:  # noqa: ANN001
    while True:
        no = f"TK-{int(token_hex(2), 16):04d}"
        exists = conn.execute(
            "SELECT 1 FROM tickets WHERE tenant_id=? AND ticket_no=?",
            (tenant, no),
        ).fetchone()
        if not exists:
            return no


async def internal_ticket_create(request):  # noqa: ANN201
    """POST /internal/tickets：坐席建工单（source=staff）。"""
    auth = auth_from_request(request)
    if not _can_write(auth):
        return JSONResponse({"error": "forbidden", "need_scope": "aftersale.write"},
                            status_code=403)
    body = await request.json()
    tenant = auth.tenant_id
    customer_id = str(body.get("customer_id") or "")
    sku = str(body.get("sku") or "")
    fault_type = str(body.get("fault_type") or "其他")
    if not customer_id or not sku:
        return JSONResponse({"error": "missing_params"}, status_code=400)
    department = str(body.get("department") or "")
    if department and department not in TICKET_DEPARTMENTS:
        return JSONResponse({"error": "bad_department"}, status_code=400)
    priority = str(body.get("priority") or "normal")
    with connect_business_db() as conn:
        exists = conn.execute(
            "SELECT 1 FROM customers WHERE tenant_id=? AND customer_id=?",
            (tenant, customer_id),
        ).fetchone()
        if exists is None:
            return JSONResponse({"error": "not_found", "detail": "customer"},
                                status_code=404)
        order_no = str(body.get("order_no") or "") or None
        if order_no:
            owned = conn.execute(
                "SELECT 1 FROM orders WHERE tenant_id=? AND order_no=? "
                "AND customer_id=?",
                (tenant, order_no.upper(), customer_id),
            ).fetchone()
            if owned is None:
                return JSONResponse({"error": "not_found", "detail": "order"},
                                    status_code=404)
        ticket_no = _gen_ticket_no(conn, tenant)
        conn.execute(
            "INSERT INTO tickets(tenant_id, ticket_no, customer_id, sku, "
            "fault_type, status, order_no, assignee_id, department, priority, "
            "source, sla_due, description) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tenant, ticket_no, customer_id, sku, fault_type, "created",
             order_no, str(body.get("assignee_id") or "") or None,
             department or None, priority, "staff",
             str(body.get("sla_due") or "") or None,
             str(body.get("description") or "") or None),
        )
    return JSONResponse({"ticket_no": ticket_no, "status": "created"},
                        status_code=201)


async def internal_ticket_list(request):  # noqa: ANN201
    """GET /internal/tickets?customer_id=&status=：坐席工单列表。"""
    auth = auth_from_request(request)
    if not _can_read(auth):
        return JSONResponse({"error": "forbidden", "need_scope": "aftersale.read"},
                            status_code=403)
    tenant = auth.tenant_id
    sql = ("SELECT ticket_no, customer_id, sku, fault_type, status, order_no, "
           "assignee_id, department, priority, sla_due, created_at FROM tickets "
           "WHERE tenant_id=?")
    args: list = [tenant]
    customer_id = request.query_params.get("customer_id") or ""
    status = request.query_params.get("status") or ""
    if customer_id:
        sql += " AND customer_id=?"
        args.append(customer_id)
    if status:
        sql += " AND status=?"
        args.append(status)
    sql += " ORDER BY created_at DESC LIMIT 50"
    with connect_business_db() as conn:
        rows = conn.execute(sql, tuple(args)).fetchall()
    return JSONResponse({"tickets": [dict(r) for r in rows]})


async def internal_ticket_patch(request):  # noqa: ANN201
    """PATCH /internal/tickets/{ticket_no}：坐席改状态/优先级/SLA 等。"""
    auth = auth_from_request(request)
    if not _can_write(auth):
        return JSONResponse({"error": "forbidden", "need_scope": "aftersale.write"},
                            status_code=403)
    ticket_no = str(request.path_params["ticket_no"] or "").upper()
    body = await request.json()
    tenant = auth.tenant_id
    fields: dict = {}
    for key in ("status", "priority", "sla_due", "description", "department"):
        if key in body:
            fields[key] = str(body.get(key) or "") or None
    if "department" in fields and fields["department"] and \
            fields["department"] not in TICKET_DEPARTMENTS:
        return JSONResponse({"error": "bad_department"}, status_code=400)
    if "status" in fields and fields["status"] and \
            fields["status"] not in TICKET_STATUSES:
        return JSONResponse({"error": "bad_status"}, status_code=400)
    if "assignee_id" in body:
        fields["assignee_id"] = str(body.get("assignee_id") or "") or None
    if not fields:
        return JSONResponse({"error": "no_fields"}, status_code=400)
    with connect_business_db() as conn:
        row = conn.execute(
            "SELECT ticket_no FROM tickets WHERE tenant_id=? AND ticket_no=?",
            (tenant, ticket_no),
        ).fetchone()
        if row is None:
            return JSONResponse({"error": "not_found"}, status_code=404)
        sets = ", ".join(f"{k}=?" for k in fields)
        conn.execute(
            f"UPDATE tickets SET {sets}, updated_at=datetime('now') "
            "WHERE tenant_id=? AND ticket_no=?",
            (*fields.values(), tenant, ticket_no),
        )
        updated = conn.execute(
            "SELECT * FROM tickets WHERE tenant_id=? AND ticket_no=?",
            (tenant, ticket_no),
        ).fetchone()
    return JSONResponse({k: updated[k] for k in updated.keys()})


async def internal_ticket_assign(request):  # noqa: ANN201
    """POST /internal/tickets/{ticket_no}/assign：坐席分派部门/坐席/优先级。"""
    auth = auth_from_request(request)
    if not _can_write(auth):
        return JSONResponse({"error": "forbidden", "need_scope": "aftersale.write"},
                            status_code=403)
    ticket_no = str(request.path_params["ticket_no"] or "").upper()
    body = await request.json()
    tenant = auth.tenant_id
    department = str(body.get("department") or "")
    if department not in TICKET_DEPARTMENTS:
        return JSONResponse({"error": "bad_department"}, status_code=400)
    with connect_business_db() as conn:
        row = conn.execute(
            "SELECT ticket_no FROM tickets WHERE tenant_id=? AND ticket_no=?",
            (tenant, ticket_no),
        ).fetchone()
        if row is None:
            return JSONResponse({"error": "not_found"}, status_code=404)
        conn.execute(
            "UPDATE tickets SET department=?, assignee_id=?, priority=?, "
            "sla_due=COALESCE(?, sla_due), status=CASE WHEN status='created' "
            "THEN 'received' ELSE status END, updated_at=datetime('now') "
            "WHERE tenant_id=? AND ticket_no=?",
            (department, str(body.get("assignee_id") or "") or None,
             str(body.get("priority") or "") or None,
             str(body.get("sla_due") or "") or None,
             tenant, ticket_no),
        )
        updated = conn.execute(
            "SELECT * FROM tickets WHERE tenant_id=? AND ticket_no=?",
            (tenant, ticket_no),
        ).fetchone()
    return JSONResponse({k: updated[k] for k in updated.keys()})


INTERNAL_ROUTES = [
    Route("/internal/tickets", internal_ticket_create, methods=["POST"]),
    Route("/internal/tickets", internal_ticket_list, methods=["GET"]),
    Route("/internal/tickets/{ticket_no}", internal_ticket_patch, methods=["PATCH"]),
    Route("/internal/tickets/{ticket_no}/assign", internal_ticket_assign,
          methods=["POST"]),
]
