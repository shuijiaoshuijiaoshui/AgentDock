"""政策域内部 API（双形态之二，PRD 3.2 / 技术方案 3.4）。

/internal/* 与 A2A Server 同进程，复用同一个检索内核（retriever.py）：
- /internal/clauses：生效条款只读查询（各域 PolicyClient 后端）
- /internal/knowledge：坐席工作台知识库 CRUD（搜索/版本化写入/历史版本）——
  逻辑单份在域内，console 只是薄代理

鉴权：走全局 JWT 中间件（Authorization: Bearer <token>）——服务间调用转发
调用方 token（on-behalf-of），tenant 从 token claim 取；用户权限由调用域自己校验。
挂载点：server.py 把 routes 并进 app。
"""

from starlette.responses import JSONResponse
from starlette.routing import Route

from domains.policy import retriever
from domains.policy.publishing import PolicyError, upsert_knowledge_entry
from shared.auth import auth_from_request
from shared.db import connect_business_db


async def internal_clauses(request):  # noqa: ANN201
    """GET /internal/clauses?q=关键词 或 ?clause_id=CLS-001 → 生效条款（只读）。"""
    auth = auth_from_request(request)
    if auth is None or not auth.has_scope("policy.read"):
        return JSONResponse({"error": "forbidden", "need_scope": "policy.read"},
                            status_code=403)
    tenant = auth.tenant_id
    clause_id = request.query_params.get("clause_id", "").strip()
    if clause_id:
        clause = retriever.get_effective_clause(clause_id.upper(), tenant_id=tenant)
        return JSONResponse({"clauses": [clause] if clause else []})
    q = request.query_params.get("q", "")
    clauses = (
        retriever.search_clauses(q, tenant_id=tenant)
        if q else retriever.list_effective(tenant_id=tenant)
    )
    return JSONResponse({"clauses": clauses})


async def internal_knowledge_search(request):  # noqa: ANN201
    """GET /internal/knowledge?q=&status=：知识条目检索/列表（坐席工作台）。"""
    auth = auth_from_request(request)
    if auth is None or not auth.has_scope("policy.read"):
        return JSONResponse({"error": "forbidden", "need_scope": "policy.read"},
                            status_code=403)
    tenant = auth.tenant_id
    q = (request.query_params.get("q") or "").strip()
    status = request.query_params.get("status") or "published"
    if q:
        rows = retriever.search_knowledge(q, limit=50, tenant_id=tenant,
                                          status=status)
    else:
        sql = ("SELECT entry_id, version, title, content, scope, status, "
               "effective_from, effective_to, conflicts_with FROM knowledge_entries "
               "WHERE tenant_id=?")
        args: list = [tenant]
        if status != "all":
            sql += " AND status=?"
            args.append(status)
        sql += " ORDER BY created_at DESC LIMIT 50"
        with connect_business_db() as conn:
            rows = [dict(r) for r in conn.execute(sql, tuple(args)).fetchall()]
    return JSONResponse({"entries": rows})


async def internal_knowledge_upsert(request):  # noqa: ANN201
    """POST /internal/knowledge：坐席补充/修正知识（版本化写）。

    发布权限：agent 只能 draft/submitted；published/archived 需主管/管理员
    （与 console 旧规则一致）。
    """
    auth = auth_from_request(request)
    if auth is None or not auth.has_scope("knowledge.write"):
        return JSONResponse({"error": "forbidden", "need_scope": "knowledge.write"},
                            status_code=403)
    body = await request.json()
    status = str(body.get("status") or "draft")
    if auth.role == "agent" and status in ("published", "archived"):
        return JSONResponse(
            {"error": "supervisor_required", "detail": "发布/归档需主管或管理员"},
            status_code=403,
        )
    try:
        result = upsert_knowledge_entry(
            auth.tenant_id, auth.actor_id,
            title=str(body.get("title") or ""),
            content=str(body.get("content") or ""),
            status=status,
            entry_id=str(body.get("entry_id") or ""),
            scope=str(body.get("scope") or "general"),
            effective_from=str(body.get("effective_from") or ""),
            effective_to=str(body.get("effective_to") or ""),
            keywords=str(body.get("keywords") or ""),
            conflicts_with=str(body.get("conflicts_with") or ""),
        )
    except PolicyError as e:
        return JSONResponse({"error": e.code, "detail": e.detail},
                            status_code=e.status)
    return JSONResponse(result, status_code=201)


async def internal_knowledge_versions(request):  # noqa: ANN201
    """GET /internal/knowledge/{entry_id}/versions：条目历史版本。"""
    auth = auth_from_request(request)
    if auth is None or not auth.has_scope("policy.read"):
        return JSONResponse({"error": "forbidden", "need_scope": "policy.read"},
                            status_code=403)
    entry_id = (request.path_params["entry_id"] or "").strip().upper()
    with connect_business_db() as conn:
        rows = conn.execute(
            "SELECT entry_id, version, title, content, scope, status, "
            "effective_from, effective_to, conflicts_with, updated_by, created_at "
            "FROM knowledge_entries WHERE tenant_id=? AND entry_id=? "
            "ORDER BY CAST(REPLACE(version, 'v', '') AS INTEGER) DESC",
            (auth.tenant_id, entry_id),
        ).fetchall()
    return JSONResponse({"versions": [dict(r) for r in rows]})


INTERNAL_ROUTES = [
    Route("/internal/clauses", internal_clauses, methods=["GET"]),
    Route("/internal/knowledge", internal_knowledge_search, methods=["GET"]),
    Route("/internal/knowledge", internal_knowledge_upsert, methods=["POST"]),
    Route("/internal/knowledge/{entry_id}/versions", internal_knowledge_versions,
          methods=["GET"]),
]
