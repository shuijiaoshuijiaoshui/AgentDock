"""人工坐席 Agent + 工作台（:8307）——A2A Server + 工作台 + 客户页。

安全基线（toB 阶段一）：
- 登录换 JWT（tenant/actor/role/scopes），HTTP 由 JWT 中间件统一验签；
- WebSocket 连接后首包认证（浏览器 WS 不能带 Authorization 头）；
- 所有会话/消息/工单/审计按 tenant 过滤；客户身份取 token.sub，不信任请求体；
- 工具、接管、审批、审计接口全部按角色 + scope 双检。
"""

import asyncio
import json
import logging
import secrets
from datetime import datetime, timezone
from pathlib import Path

from starlette.applications import Starlette
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import TaskState

import a2a.helpers as ah
import re

from console import attachments as attachment_store
from console import db, tools as console_tools
from domains.policy import ingestion as policy_ingestion
from domains.policy import publishing as policy_publishing
from shared import approvals, ratelimit
from shared.actions import ACTION_REGISTRY, resolve_action
from shared.a2a_client import delegate
from shared.a2a_langgraph import build_a2a_app, build_agent_card, request_metadata
from shared.audit import audit_event, audit_from_auth, list_events, verify_chain
from shared.auth import (
    COOKIE_SURFACES,
    AuthContext,
    AuthError,
    auth_from_cookie_values,
    auth_from_headers,
    auth_from_request,
    build_auth_middleware,
    cookie_prefix,
    create_token,
    csrf_cookie_name,
    decode_token,
    session_cookie_name,
)
from shared.config import get_settings
from shared.pii import mask_pii
from shared.security_headers import SecurityHeadersMiddleware
from shared.tracing import (
    build_span_tree,
    new_trace_id,
    read_trace_spans,
    start_server_span,
)

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


def _evt(cid: str, message: dict | None = None, type_: str = "message",
         tenant: str = db.DEFAULT_TENANT) -> dict:
    payload: dict = {"type": type_, "conversation": db.get_conversation(cid, tenant_id=tenant)}
    if message:
        payload["message"] = message
    return payload


def _client_ip(request) -> str:  # noqa: ANN001
    return request.client.host if getattr(request, "client", None) else ""


def _ua(request) -> str:  # noqa: ANN001
    return request.headers.get("user-agent", "")[:200]


def _forbidden(detail: str = "forbidden") -> JSONResponse:
    return JSONResponse({"error": detail}, status_code=403)


def _rate_limit(auth: AuthContext, request, bucket: str = "http"):  # noqa: ANN001
    ok, retry = ratelimit.check_user(auth.tenant_id, auth.actor_id, bucket)
    if ok:
        return None
    audit_event(
        tenant_id=auth.tenant_id, actor_id=auth.actor_id, actor_role=auth.role,
        action="rate_limit.block", target=request.url.path,
        ip=_client_ip(request), user_agent=_ua(request),
    )
    return JSONResponse(
        {"error": "rate_limited", "retry_after": retry},
        status_code=429, headers={"Retry-After": str(retry)},
    )


def _conv_for(auth: AuthContext, cid: str, *, write: bool = False) -> dict:
    conv = db.get_conversation(cid, tenant_id=auth.tenant_id)
    if conv is None:
        raise KeyError(cid)
    if auth.role == "customer":
        if conv.get("customer_id") != auth.actor_id:
            raise PermissionError("not your conversation")
    elif auth.role == "agent":
        assigned = conv.get("assigned_agent_id")
        if assigned and assigned != auth.actor_id and conv.get("mode") == "human":
            raise PermissionError("assigned to another agent")
    return conv


class HumanDeskExecutor(AgentExecutor):
    """handoff.accept：收交接包 → 入坐席待接管队列 → completed。"""

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        await event_queue.enqueue_event(
            ah.new_task(
                task_id=context.task_id,
                context_id=context.context_id,
                state=TaskState.TASK_STATE_SUBMITTED,
            )
        )
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.start_work()
        headers = {}
        try:
            headers = (context.call_context.state or {}).get("headers") or {}
        except Exception:  # noqa: BLE001
            pass
        try:
            auth = auth_from_headers(headers)
        except AuthError:
            auth = None
        tenant = auth.tenant_id if auth else ""
        # M4（PRD 7.10）：交接委托也进 span 树（父 = 编排 handoff 节点的 span）
        req_meta = request_metadata(context)
        span_cm = start_server_span(
            "console.handoff_accept",
            trace_id=str(req_meta.get("trace_id", "")),
            parent_traceparent=str(req_meta.get("traceparent", "")),
            attributes={"a2a.context_id": context.context_id,
                        "a2a.task_id": context.task_id},
        )
        with span_cm:
            await self._handle(context, event_queue, updater, auth, tenant)

    async def _handle(self, context, event_queue, updater, auth, tenant) -> None:  # noqa: ANN001
        if auth is None or not auth.has_scope("handoff.accept"):
            audit_from_auth(
                auth, "handoff.rejected", target=context.context_id or "",
                result_summary="缺少 handoff.accept scope",
                context_id=context.context_id or "", task_id=context.task_id or "",
            )
            await updater.failed(
                ah.new_text_message(
                    "handoff rejected", context_id=context.context_id,
                    task_id=context.task_id,
                )
            )
            return
        try:
            package = json.loads(context.get_user_input() or "{}")
        except json.JSONDecodeError:
            package = {"context_id": context.context_id, "reasons": ["交接包解析失败"],
                       "user_text": context.get_user_input() or ""}
        package.setdefault("context_id", context.context_id)
        package.setdefault("tenant_id", tenant)
        handoff_id = db.enqueue_handoff(package, tenant_id=tenant)
        if db.get_conversation(package["context_id"], tenant_id=tenant):
            db.update_conversation(
                package["context_id"], tenant_id=tenant, status="pending_handoff"
            )
        audit_from_auth(
            auth, "handoff.accept", target=package["context_id"],
            params={"handoff_id": handoff_id, "reasons": package.get("reasons", [])},
            context_id=package["context_id"], task_id=context.task_id or "",
        )
        await updater.add_artifact(
            [ah.new_data_part({"type": "handoff_ack", "handoff_id": handoff_id})],
            name="humandesk.ack",
        )
        await updater.complete()

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        await event_queue.enqueue_event(
            ah.new_task(
                task_id=context.task_id,
                context_id=context.context_id,
                state=TaskState.TASK_STATE_SUBMITTED,
            )
        )
        await TaskUpdater(event_queue, context.task_id, context.context_id).cancel()


class ConvSockets:
    def __init__(self) -> None:
        self._by_conv: dict[str, set[WebSocket]] = {}

    async def connect(self, conv_id: str, ws: WebSocket) -> None:
        await ws.accept()
        self.add(conv_id, ws)

    def add(self, conv_id: str, ws: WebSocket) -> None:
        self._by_conv.setdefault(conv_id, set()).add(ws)

    def disconnect(self, conv_id: str, ws: WebSocket) -> None:
        self._by_conv.get(conv_id, set()).discard(ws)

    async def broadcast(self, conv_id: str, payload: dict) -> None:
        for ws in list(self._by_conv.get(conv_id, ())):
            try:
                await ws.send_json(payload)
            except Exception:  # noqa: BLE001
                self.disconnect(conv_id, ws)

    def count(self, conv_id: str) -> int:
        return len(self._by_conv.get(conv_id, ()))


manager = ConvSockets()


# ---------------------------------------------------------------------------
# 聊天处理
# ---------------------------------------------------------------------------

CONV_STATES = ("AI_ACTIVE", "PENDING_HANDOFF", "HUMAN_ACTIVE",
               "PENDING_REVIEW", "CLOSED")
# 复核策略：涉资金写链完结后强制 PENDING_REVIEW（口径见 docs/统计口径.md）
REVIEW_INTENTS = {"cancel_refund", "refund_settle", "invoice_issue",
                  "order_price_amend"}
_STATE_STATUS = {
    "AI_ACTIVE": "active", "PENDING_HANDOFF": "pending_handoff",
    "HUMAN_ACTIVE": "with_human", "PENDING_REVIEW": "review",
    "CLOSED": "closed",
}


async def _set_conv_state(cid: str, state: str, auth: AuthContext, *, note: str = ""):
    """会话状态机唯一入口：状态列 + 兼容 status + 审计 + WS 推送。"""
    if state not in CONV_STATES:
        raise ValueError(f"unknown state {state}")
    db.update_conversation(
        cid, tenant_id=auth.tenant_id, state=state,
        status=_STATE_STATUS[state],
    )
    audit_from_auth(
        auth, "conversation.state", target=cid,
        params={"state": state, "note": note}, context_id=cid,
    )
    await manager.broadcast(cid, {
        "type": "state",
        "conversation": db.get_conversation(cid, tenant_id=auth.tenant_id),
    })


def _bind_message_attachments(
    cid: str, message_id: int, attachment_ids: list, auth: AuthContext,
) -> list[dict]:
    """校验附件归属（同租户 + 上传者/可见会话）并绑定到消息。"""
    bound: list[dict] = []
    for raw in attachment_ids or []:
        attachment_id = str(raw or "")
        row = db.get_attachment(attachment_id, tenant_id=auth.tenant_id)
        if row is None:
            raise PermissionError(f"attachment {attachment_id} not found")
        if not attachment_store.can_download(
            row, auth,
            conversation_lookup=lambda c: db.get_conversation(
                c, tenant_id=auth.tenant_id),
        ):
            raise PermissionError(f"attachment {attachment_id} forbidden")
        db.bind_attachment(attachment_id, message_id, cid,
                           tenant_id=auth.tenant_id)
        bound.append({
            "attachment_id": attachment_id, "filename": row["filename"],
            "mime": row["mime"], "size": row["size"],
        })
    return bound


def _steps_from_artifact(data: dict) -> list[dict]:
    """把编排 final_answer 的 decision_log + artifacts 映射为步骤时间线。"""
    steps: list[dict] = [{
        "stage": "intake", "status": "completed",
        "output_summary": f"intent={data.get('intent', '')}",
    }]
    for entry in data.get("decision_log") or []:
        if not isinstance(entry, dict):
            continue
        stage = str(entry.get("stage") or "")
        if stage == "domain_call":
            steps.append({
                "stage": "domain_call", "domain": entry.get("domain"),
                "tool": entry.get("skill"),
                "input_summary": entry.get("input_summary"),
                "output_summary": entry.get("output_summary"),
                "latency_ms": entry.get("latency_ms"),
                "status": entry.get("state") or "completed",
                "error": entry.get("error"),
            })
        elif stage == "understand":
            steps.append({
                "stage": "understand", "status": "completed",
                "output_summary": "澄清：" + str(entry.get("reply", ""))[:150],
            })
        elif stage == "route":
            steps.append({
                "stage": "route", "status": "completed",
                "output_summary": (
                    f"{entry.get('intent')} → {entry.get('domains')} "
                    f"({entry.get('mode')})"
                ),
            })
        elif stage in ("arbitrate_value", "no_conflict", "veto"):
            steps.append({
                "stage": "arbitrate", "status": "completed",
                "output_summary": json.dumps(entry, ensure_ascii=False)[:300],
            })
        elif stage == "saga_compensate":
            steps.append({
                "stage": "saga_compensate",
                "status": "completed" if entry.get("ok") else "error",
                "error": "" if entry.get("ok") else "补偿失败",
                "output_summary": json.dumps(entry, ensure_ascii=False)[:300],
            })
        elif stage == "synthesize":
            steps.append({
                "stage": "synthesize", "model": entry.get("model"),
                "prompt_version": entry.get("prompt_version"),
                "status": "completed",
                "output_summary": entry.get("output_summary"),
            })
    for domain, artifact in (data.get("artifacts") or {}).items():
        if not isinstance(artifact, dict):
            continue
        for event in artifact.get("tool_events") or []:
            if not isinstance(event, dict):
                continue
            steps.append({
                "stage": "tool", "domain": event.get("domain") or domain,
                "tool": event.get("tool"),
                "input_summary": event.get("input_summary"),
                "output_summary": event.get("output_summary"),
                "latency_ms": event.get("latency_ms"),
                "status": event.get("status") or "completed",
                "error": event.get("error"),
            })
        steps.append({
            "stage": "deliver", "domain": domain, "tool": artifact.get("skill"),
            "status": "completed",
            "output_summary": (
                f"authority={artifact.get('authority_domain')} "
                f"as_of={artifact.get('as_of')}"
            ),
        })
    return steps


def _persist_agent_run(
    cid: str, auth: AuthContext, data: dict, trace_id: str, started_at: str,
) -> dict | None:
    try:
        return db.record_agent_run(
            cid, tenant_id=auth.tenant_id, trace_id=str(data.get("trace_id") or trace_id),
            intent=str(data.get("intent") or ""),
            model=str(data.get("model") or ""),
            prompt_version=str(data.get("prompt_version") or ""),
            status="completed",
            steps=_steps_from_artifact(data),
            started_at=started_at,
        )
    except Exception:  # noqa: BLE001
        logger.exception("agent run 落库失败 conv=%s", cid)
        return None


def _expired(expires_at: str) -> bool:
    if not expires_at:
        return False
    try:
        return datetime.fromisoformat(expires_at) < datetime.now(timezone.utc)
    except ValueError:
        return True


def _pending_from_artifacts(
    data: dict | None, cid: str, auth: AuthContext, trace_id: str
) -> list[dict]:
    """把域 Artifact 的 pending_confirm/pending_approval 落成会话级待办。"""
    if not isinstance(data, dict):
        return []
    out: list[dict] = []
    existing = db.list_pending_actions(cid, tenant_id=auth.tenant_id)
    for domain, artifact in (data.get("artifacts") or {}).items():
        if not isinstance(artifact, dict):
            continue
        d = artifact.get("data") or {}
        if not isinstance(d, dict):
            continue  # 列表型产物（对账单/退款单等）不产生确认卡
        status = d.get("status")
        if status not in ("pending_confirm", "pending_approval"):
            continue
        skill = str(artifact.get("skill") or "")
        preview = d.get("preview") or {}
        ptype = "confirm" if status == "pending_confirm" else "approval"
        if skill == "order.amend":
            order_no = str(preview.get("order_no") or d.get("order_no") or "")
            price = preview.get("new_price", d.get("new_price"))
            if price is not None:
                intent = "order_price_amend"
                slots = {"order_no": order_no, "new_price": price}
            else:
                intent = "order_amend"
                slots = {"order_no": order_no,
                         "address": preview.get("new_address", "")}
        elif skill == "ticket.create":
            intent = "ticket_create"
            slots = {"sku": preview.get("sku", ""),
                     "fault_type": preview.get("fault_type", "")}
        elif skill == "invoice.issue":
            intent = "invoice_issue"
            slots = {"order_no": preview.get("order_no") or d.get("order_no") or "",
                     "title": preview.get("title") or d.get("title") or "默认抬头"}
        elif skill == "refund.settle":
            intent = "refund_settle"
            slots = {"order_no": d.get("order_no") or d.get("target") or ""}
        else:
            continue
        token = str(d.get("confirm_token") or "")
        aid = str(d.get("approval_id") or "")
        duplicate = next(
            (e for e in existing
             if (token and e.get("confirm_token") == token)
             or (aid and e.get("approval_id") == aid)),
            None,
        )
        if duplicate is not None:
            out.append({
                "action_id": duplicate.get("action_id"),
                "type": duplicate.get("type"),
                "preview": duplicate.get("preview"),
                "expires_at": duplicate.get("expires_at"),
            })
            continue
        action = db.create_pending_action(
            cid, type=ptype,
            payload={"intent": intent, "slots": slots},
            tenant_id=auth.tenant_id, domain=domain, skill=skill,
            preview=preview, confirm_token=token,
            approval_id=aid,
            expires_at=str(d.get("expires_at") or ""), trace_id=trace_id,
        )
        out.append({
            "action_id": action.get("action_id"),
            "type": action.get("type"),
            "preview": action.get("preview"),
            "expires_at": action.get("expires_at"),
        })
    return out


async def _resume_pending_action(
    pending: dict, auth: AuthContext, conv: dict
) -> dict:
    cid = pending["conversation_id"]
    if pending.get("type") == "confirm" and auth.role != "customer":
        return {"ok": False, "error": "confirm_requires_owner", "status": 403}
    if auth.role == "customer" and conv.get("customer_id") != auth.actor_id:
        return {"ok": False, "error": "not_found", "status": 404}
    if pending.get("status") != "pending":
        return {"ok": False, "error": f"status_{pending.get('status')}", "status": 409}
    if _expired(str(pending.get("expires_at") or "")):
        db.update_pending_action_status(
            pending["action_id"], "expired", tenant_id=auth.tenant_id,
        )
        return {"ok": False, "error": "expired", "status": 409}
    s = get_settings()
    action_payload = pending.get("payload") or {}
    # 短时 action token：aud=orchestrator / typ=action，绑定会话与 intent/slots。
    # 编排侧验签通过才允许 forced intent；metadata 本身不是信任边界。
    action_token = create_token(
        tenant_id=auth.tenant_id,
        actor_id=auth.actor_id,
        role=auth.role,
        scopes=sorted(auth.scopes),
        ttl_seconds=120,
        audience="orchestrator",
        token_type="action",
        extra_claims={
            "conversation_id": cid,
            "action_id": str(pending.get("action_id") or ""),
            "intent": str(action_payload.get("intent") or ""),
            "slots": action_payload.get("slots") or {},
        },
    )
    meta = {
        "trace_id": new_trace_id(),
        "context_id": cid,
        "conversation_id": cid,
        "customer_id": conv.get("customer_id") or (auth.actor_id if auth.role == "customer" else ""),
        "tenant_id": auth.tenant_id,
        "actor_id": auth.actor_id,
        "action_token": action_token,
        "confirm_token": pending.get("confirm_token") or "",
        "approval_id": pending.get("approval_id") or "",
    }
    result = await delegate(
        s.domain_url("orchestrator"), "继续执行", context_id=cid,
        metadata=meta, auth=auth,
    )
    if result.state == TaskState.TASK_STATE_INPUT_REQUIRED:
        payload = result.status_data or {}
        question = str(payload.get("question", "请补充一下您的需求。"))
        db.update_conversation(cid, tenant_id=auth.tenant_id,
                               pending_task_id=result.task_id)
        m = db.add_message(cid, "ai", question,
                           meta={"type": "clarify", "payload": payload},
                           tenant_id=auth.tenant_id)
        await manager.broadcast(cid, _evt(cid, m, tenant=auth.tenant_id))
        return {"ok": True, "type": "clarify", "reply": question}
    if result.state != TaskState.TASK_STATE_COMPLETED:
        return {"ok": False, "error": TaskState.Name(result.state), "status": 502}
    data = result.data_parts[0] if result.data_parts else {}
    reply = result.text or (
        data.get("final_text") if isinstance(data, dict) else ""
    ) or "（AI 暂未返回内容，请重试）"
    pending2 = _pending_from_artifacts(data, cid, auth, meta["trace_id"])
    run = None
    if isinstance(data, dict) and data.get("type") == "final_answer":
        run = _persist_agent_run(cid, auth, data, str(meta.get("trace_id", "")),
                                 "")
        intent = str(data.get("intent") or "")
        if intent in REVIEW_INTENTS and conv.get("state") == "AI_ACTIVE":
            await _set_conv_state(cid, "PENDING_REVIEW", auth,
                                  note=f"auto_review:{intent}")
    m = db.add_message(
        cid, "ai", reply,
        meta={"run_id": (run or {}).get("run_id"),
              "pending_actions": pending2} if (run or pending2) else None,
        tenant_id=auth.tenant_id,
    )
    db.update_pending_action_status(
        pending["action_id"], "cancelled" if pending2 else "executed",
        tenant_id=auth.tenant_id,
    )
    await manager.broadcast(cid, _evt(cid, m, tenant=auth.tenant_id))
    return {"ok": True, "reply": reply, "pending_actions": pending2}


def _build_history_meta(cid: str, auth: AuthContext) -> dict:
    """组装跨轮历史（M4 / PRD 10.4）：滚动摘要 + 最近 N 轮原文 + 掉出窗口的轮次。

    - summary/upto_message_id：上一轮编排侧生成、本表落库的滚动摘要；
    - recent_turns：最近 N 条 customer/ai/agent 消息（每轮截断 + 再过一遍
      mask_pii——落库前已脱敏，这里防历史拼接时的二次泄漏）；
    - carry_turns：摘要覆盖点之后、掉出最近窗口之前的消息（编排侧据此做增量摘要）。
    """
    s = get_settings()
    recent_n = max(1, s.history_recent_turns)
    max_chars = max(40, s.history_turn_max_chars)
    row = db.get_session_summary(cid, tenant_id=auth.tenant_id) or {}
    upto = int(row.get("upto_message_id") or 0)
    msgs = [m for m in db.list_messages(cid, tenant_id=auth.tenant_id)
            if m.get("role") in ("customer", "ai", "agent")]

    def _turn(m: dict) -> dict:
        text = mask_pii((m.get("content") or "")[:max_chars])[0]
        return {"id": int(m["id"]), "role": m["role"], "text": text}

    recent = [_turn(m) for m in msgs[-recent_n:]]
    window_first = recent[0]["id"] if recent else 0
    carry = [_turn(m) for m in msgs if upto < int(m["id"]) < window_first]
    return {
        "summary": str(row.get("summary") or ""),
        "upto_message_id": upto,
        "recent_turns": recent,
        "carry_turns": carry[-12:],
    }


async def handle_customer_message(
    cid: str, text: str, auth: AuthContext, customer_id: str | None = None,
    attachment_ids: list[str] | None = None,
) -> dict:
    conv = _conv_for(auth, cid)
    if conv.get("state") == "CLOSED" or conv.get("status") == "closed":
        raise PermissionError("会话已结束，不能继续发言")
    if auth.role == "customer":
        customer_id = auth.actor_id
        if conv.get("customer_id") and conv["customer_id"] != customer_id:
            raise PermissionError("not your conversation")
        if not conv.get("customer_id"):
            db.update_conversation(cid, tenant_id=auth.tenant_id, customer_id=customer_id)
            conv = db.get_conversation(cid, tenant_id=auth.tenant_id)
    text, _ = mask_pii(text)
    msg = db.add_message(cid, "customer", text, tenant_id=auth.tenant_id)
    if attachment_ids:
        bound = _bind_message_attachments(cid, msg["id"], attachment_ids, auth)
        db.set_message_meta(msg["id"], {"attachments": bound},
                            tenant_id=auth.tenant_id)
        msg = db.get_message(msg["id"], tenant_id=auth.tenant_id) or msg
    await manager.broadcast(cid, _evt(cid, msg, tenant=auth.tenant_id))

    if conv["mode"] == "human":
        return {"conversation_id": cid, "mode": "human", "reply": None}

    s = get_settings()
    orch_url = s.domain_url("orchestrator")
    meta = {
        "trace_id": new_trace_id(),
        "context_id": cid,
        "customer_id": conv.get("customer_id") or (customer_id or ""),
        "hit_wall_count": str(conv["wall_count"]),
        "tenant_id": auth.tenant_id,
        "actor_id": auth.actor_id,
        # M4（PRD 10.4）：跨轮历史随 metadata 续传（碰壁计数同款渠道层通道）
        "history": _build_history_meta(cid, auth),
    }
    # M4（PRD 7.10）：渠道层根 span——trace_id 与审计/agent_runs 同号，
    # delegate 在此 span 活动期间把 traceparent 注入请求 metadata。
    with start_server_span(
        "console.customer_turn",
        trace_id=str(meta["trace_id"]),
        attributes={"a2a.context_id": cid, "actor_id": auth.actor_id},
    ):
        result = await delegate(
            orch_url, text,
            task_id=conv.get("pending_task_id"),
            context_id=cid,
            metadata=meta,
            auth=auth,
        )

    if result.state == TaskState.TASK_STATE_INPUT_REQUIRED:
        payload = result.status_data or {}
        question = str(payload.get("question", "请补充一下您的需求。"))
        db.update_conversation(
            cid, tenant_id=auth.tenant_id, pending_task_id=result.task_id
        )
        m = db.add_message(
            cid, "ai", question, meta={"type": "clarify", "payload": payload},
            tenant_id=auth.tenant_id,
        )
        await manager.broadcast(cid, _evt(cid, m, tenant=auth.tenant_id))
        return {"conversation_id": cid, "type": "clarify", "reply": question}

    db.update_conversation(cid, tenant_id=auth.tenant_id, pending_task_id=None)
    data = result.data_parts[0] if result.data_parts else {}
    reply = result.text or (
        data.get("final_text") if isinstance(data, dict) else ""
    ) or "（AI 暂未返回内容，请重试）"
    pending = _pending_from_artifacts(
        data, cid, auth, str(meta.get("trace_id", "")),
    ) if isinstance(data, dict) else []
    cards = _cards_from_artifacts(data) if isinstance(data, dict) else []
    m = db.add_message(
        cid, "ai", reply,
        meta={"cards": cards, "pending_actions": pending}
        if (cards or pending) else None,
        tenant_id=auth.tenant_id,
    )
    if isinstance(data, dict):
        if data.get("handoff"):
            db.update_conversation(cid, tenant_id=auth.tenant_id, status="pending_handoff")
            await _set_conv_state(cid, "PENDING_HANDOFF", auth, note="ai_handoff")
        if data.get("hit_wall_count") is not None:
            db.update_conversation(
                cid, tenant_id=auth.tenant_id, wall_count=int(data["hit_wall_count"])
            )
        # M4（PRD 10.4）：编排侧生成的滚动摘要落库，下一轮随 metadata 注入
        ss = data.get("session_summary")
        if isinstance(ss, dict) and ss.get("summary"):
            db.upsert_session_summary(
                cid, str(ss["summary"]), int(ss.get("upto_message_id") or 0),
                tenant_id=auth.tenant_id,
            )
    run = None
    if isinstance(data, dict) and data.get("type") == "final_answer":
        run = _persist_agent_run(
            cid, auth, data, str(meta.get("trace_id", "")),
            str(m.get("created_at") or ""),
        )
        # 资金写链自动进复核（策略口径见 docs/统计口径.md）
        intent = str(data.get("intent") or "")
        if intent in REVIEW_INTENTS and conv.get("state") == "AI_ACTIVE":
            await _set_conv_state(cid, "PENDING_REVIEW", auth,
                                  note=f"auto_review:{intent}")
    await manager.broadcast(cid, _evt(cid, m, tenant=auth.tenant_id))
    return {"conversation_id": cid, "type": "ai", "reply": reply,
            "run_id": (run or {}).get("run_id"),
            "pending_actions": pending}


async def handle_agent_reply(cid: str, text: str, auth: AuthContext) -> dict:
    conv = _conv_for(auth, cid)
    if conv.get("state") == "CLOSED" or conv.get("status") == "closed":
        raise PermissionError("会话已结束，不能继续回复")
    if conv["mode"] != "human":
        raise PermissionError("会话不在人工模式")
    if auth.role == "agent" and conv.get("assigned_agent_id") not in (None, auth.actor_id):
        raise PermissionError("assigned to another agent")
    text, _ = mask_pii(text)
    msg = db.add_message(cid, "agent", text, tenant_id=auth.tenant_id)
    audit_from_auth(
        auth, "console.agent_reply", target=cid,
        params={"conversation_id": cid}, result_summary=text[:200],
        context_id=cid,
    )
    await manager.broadcast(cid, _evt(cid, msg, tenant=auth.tenant_id))
    return msg


# ---------------------------------------------------------------------------
# 静态页 + 登录
# ---------------------------------------------------------------------------

async def page_workbench(request):  # noqa: ANN201
    return FileResponse(STATIC_DIR / "workbench.html")


async def page_customer(request):  # noqa: ANN201
    return FileResponse(STATIC_DIR / "customer.html")


async def page_portal(request):  # noqa: ANN201
    """演示用商家后台壳（与客服页同源，不依赖跨域 iframe）。"""
    return FileResponse(STATIC_DIR / "portal.html")


async def api_auth_config(request):  # noqa: ANN201
    """登录页元数据：租户列表 + dev 演示账号（prod 不返回租户/账号）。"""
    s = get_settings()
    payload: dict = {}
    if s.env != "prod":
        payload["tenants"] = db.list_tenants()
    if s.env == "dev" and s.dev_mode:
        payload["demo_password"] = "demo123"
        payload["demo_accounts"] = [
            {"username": "customer", "role": "customer", "tenant": "t_demo"},
            {"username": "agent", "role": "agent", "tenant": "t_demo"},
            {"username": "supervisor", "role": "supervisor", "tenant": "t_demo"},
            {"username": "finance", "role": "finance", "tenant": "t_demo"},
            {"username": "admin", "role": "admin", "tenant": "t_demo"},
        ]
    return JSONResponse(payload)


async def api_login(request):  # noqa: ANN201
    body = await request.json()
    tenant = str(body.get("tenant_id") or "t_demo")
    username = str(body.get("username") or "").strip()
    password = str(body.get("password") or "")
    surface = str(body.get("surface") or "customer")
    if surface not in COOKIE_SURFACES:
        surface = "customer"
    ok, retry = ratelimit.check(f"login:{_client_ip(request)}", limit=10)
    if not ok:
        return JSONResponse(
            {"error": "rate_limited", "retry_after": retry},
            status_code=429, headers={"Retry-After": str(retry)},
        )
    user = db.authenticate(tenant, username, password)
    if not user:
        audit_event(
            tenant_id=tenant, actor_id=username or "anonymous", actor_role="anonymous",
            action="auth.login_failed", target="console",
            ip=_client_ip(request), user_agent=_ua(request),
        )
        return JSONResponse({"error": "invalid_credentials"}, status_code=401)
    token = create_token(
        tenant_id=tenant, actor_id=user["user_id"], role=user["role"],
        scopes=user["scopes"],
    )
    audit_event(
        tenant_id=tenant, actor_id=user["user_id"], actor_role=user["role"],
        action="auth.login", target="console", result_summary="ok",
        ip=_client_ip(request), user_agent=_ua(request),
    )
    s = get_settings()
    secure = s.env == "prod"
    max_age = s.auth_token_ttl_minutes * 60
    csrf_token = secrets.token_urlsafe(24)
    response = JSONResponse({
        "token": token,  # API 客户端用；浏览器只用 HttpOnly cookie
        "csrf_token": csrf_token,
        "surface": surface,
        "user": {
            "tenant_id": tenant, "user_id": user["user_id"],
            "username": user["username"], "role": user["role"],
            "display_name": user["display_name"], "scopes": user["scopes"],
        },
    })
    response.set_cookie(
        session_cookie_name(surface), token,
        max_age=max_age, httponly=True, samesite="lax", secure=secure,
        path=s.cookie_path,
    )
    response.set_cookie(
        csrf_cookie_name(surface), csrf_token,
        max_age=max_age, httponly=False, samesite="lax", secure=secure,
        path=s.cookie_path,
    )
    return response


async def api_logout(request):  # noqa: ANN201
    auth = auth_from_request(request)
    if auth is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    s = get_settings()
    response = JSONResponse({"ok": True})
    for surface in COOKIE_SURFACES:
        response.delete_cookie(
            session_cookie_name(surface), path=s.cookie_path,
        )
        response.delete_cookie(
            csrf_cookie_name(surface), path=s.cookie_path,
        )
    audit_from_auth(auth, "auth.logout", target="console")
    return response


async def api_me(request):  # noqa: ANN201
    auth = auth_from_request(request)
    if auth is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    user = db.get_user(auth.tenant_id, auth.actor_id) or {}
    surface = getattr(request.state, "auth_surface", "") or "customer"
    return JSONResponse({
        "tenant_id": auth.tenant_id, "user_id": auth.actor_id,
        "role": auth.role, "scopes": sorted(auth.scopes),
        "display_name": user.get("display_name", ""),
        "surface": surface,
        "csrf_token": request.cookies.get(csrf_cookie_name(surface), ""),
    })


# ---------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------

def _customer_names(tenant_id: str) -> dict:
    from shared.db import connect_business_db

    try:
        conn = connect_business_db()
        rows = conn.execute(
            "SELECT customer_id, name FROM customers WHERE tenant_id=?", (tenant_id,)
        ).fetchall()
        return {r["customer_id"]: r["name"] for r in rows}
    except Exception:  # noqa: BLE001
        return {}


async def api_conversations(request):  # noqa: ANN201
    auth = auth_from_request(request)
    if auth is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    limited = _rate_limit(auth, request)
    if limited:
        return limited
    names = _customer_names(auth.tenant_id)
    if auth.role == "customer":
        rows = [
            c for c in db.list_conversations(tenant_id=auth.tenant_id)
            if c.get("customer_id") == auth.actor_id
        ]
    elif auth.role == "agent":
        rows = db.list_conversations(
            tenant_id=auth.tenant_id, assigned_agent_id=auth.actor_id
        )
    elif auth.role in ("supervisor", "admin"):
        rows = db.list_conversations(tenant_id=auth.tenant_id)
    else:
        return _forbidden()
    out = []
    for c in rows:
        cid = c.get("customer_id")
        c["customer_name"] = names.get(cid, "") if cid else ""
        out.append(c)
    return JSONResponse(out)


async def api_messages(request):  # noqa: ANN201
    auth = auth_from_request(request)
    if auth is None or auth.role not in ("customer", "agent", "supervisor", "admin"):
        return _forbidden()
    cid = request.path_params["cid"]
    try:
        _conv_for(auth, cid)
    except KeyError:
        return JSONResponse({"error": "not_found"}, status_code=404)
    except PermissionError:
        return _forbidden()
    after = int(request.query_params.get("after_id", 0))
    return JSONResponse(db.list_messages(cid, after, tenant_id=auth.tenant_id))


async def api_handoff(request):  # noqa: ANN201
    auth = auth_from_request(request)
    if auth is None or auth.role not in ("customer", "agent", "supervisor", "admin"):
        return _forbidden()
    cid = request.path_params["cid"]
    try:
        _conv_for(auth, cid)
    except KeyError:
        return JSONResponse({"error": "not_found"}, status_code=404)
    except PermissionError:
        return _forbidden()
    handoff = db.get_handoff(cid, tenant_id=auth.tenant_id)
    if handoff is None:
        return JSONResponse(None)
    if auth.role == "customer":
        # 客户只能看对外状态/原因；ai_draft/decision_log/domain_artifacts/history 属内部。
        package = handoff.get("package") or {}
        return JSONResponse({
            "status": handoff.get("status"),
            "reasons": package.get("reasons", []),
            "created_at": handoff.get("created_at"),
        })
    return JSONResponse(handoff)


async def api_takeover(request):  # noqa: ANN201
    auth = auth_from_request(request)
    if auth is None or auth.role not in ("agent", "supervisor", "admin"):
        return _forbidden()
    cid = request.path_params["cid"]
    try:
        conv = _conv_for(auth, cid)
    except KeyError:
        return JSONResponse({"error": "not_found"}, status_code=404)
    except PermissionError:
        return _forbidden()
    if conv["status"] != "pending_handoff":
        return JSONResponse({"error": f"状态 {conv['status']} 不可接管"}, status_code=409)
    db.take_handoff(cid, tenant_id=auth.tenant_id, taken_by=auth.actor_id)
    db.update_conversation(
        cid, tenant_id=auth.tenant_id, mode="human",
        assigned_agent_id=auth.actor_id,
    )
    await _set_conv_state(cid, "HUMAN_ACTIVE", auth, note="takeover")
    msg = db.add_message(
        cid, "system", "人工客服已接入，正在为您服务。", tenant_id=auth.tenant_id
    )
    audit_from_auth(
        auth, "console.takeover", target=cid, params={"conversation_id": cid},
        context_id=cid,
    )
    await manager.broadcast(cid, _evt(cid, msg, tenant=auth.tenant_id))
    return JSONResponse(db.get_conversation(cid, tenant_id=auth.tenant_id))


async def api_return(request):  # noqa: ANN201
    auth = auth_from_request(request)
    if auth is None or auth.role not in ("agent", "supervisor", "admin"):
        return _forbidden()
    cid = request.path_params["cid"]
    try:
        conv = _conv_for(auth, cid)
    except KeyError:
        return JSONResponse({"error": "not_found"}, status_code=404)
    if conv["mode"] != "human":
        return JSONResponse({"error": "会话不在人工模式"}, status_code=409)
    if auth.role == "agent" and conv.get("assigned_agent_id") not in (None, auth.actor_id):
        return _forbidden("assigned to another agent")
    db.update_conversation(
        cid, tenant_id=auth.tenant_id, mode="ai", assigned_agent_id=None,
    )
    await _set_conv_state(cid, "AI_ACTIVE", auth, note="return_to_ai")
    msg = db.add_message(
        cid, "system", "已交回 AI 助手继续服务。", tenant_id=auth.tenant_id
    )
    audit_from_auth(
        auth, "console.return", target=cid, params={"conversation_id": cid},
        context_id=cid,
    )
    await manager.broadcast(cid, _evt(cid, msg, tenant=auth.tenant_id))
    return JSONResponse(db.get_conversation(cid, tenant_id=auth.tenant_id))


async def api_customer_conversation(request):  # noqa: ANN201
    """客户进入页：取或建当前会话，返回客户名与自动问候（不重复问候）。"""
    auth = auth_from_request(request)
    if auth is None or auth.role != "customer":
        return _forbidden()
    tenant = auth.tenant_id
    convs = [
        c for c in db.list_conversations(tenant_id=tenant, customer_id=auth.actor_id)
        if c.get("state") != "CLOSED"
    ]
    created = False
    if convs:
        conv = convs[0]
    else:
        conv = db.create_conversation(auth.actor_id, tenant_id=tenant)
        created = True
    cid = conv["id"]
    messages = db.list_messages(cid, tenant_id=tenant)
    if created or not any(m["role"] == "ai" for m in messages):
        user = db.get_user(tenant, auth.actor_id) or {}
        name = user.get("display_name") or auth.actor_id
        greeting = (
            f"{name}您好，我是商家经营助手。可以帮您查订单、库存、账期与对账、"
            "售后资格，也能直接发起售后或转人工。"
        )
        db.add_message(cid, "ai", greeting, meta={"type": "greeting"},
                       tenant_id=tenant)
        messages = db.list_messages(cid, tenant_id=tenant)
    return JSONResponse({
        "conversation": db.get_conversation(cid, tenant_id=tenant),
        "messages": messages[-30:],
        "has_more": len(messages) > 30,
        "customer": {"customer_id": auth.actor_id,
                     "display_name": (db.get_user(tenant, auth.actor_id) or {})
                     .get("display_name", "")},
    })


async def api_conversation_history(request):  # noqa: ANN201
    """客户历史会话分页（只返回自己的）。"""
    auth = auth_from_request(request)
    if auth is None or auth.role != "customer":
        return _forbidden()
    limit = min(int(request.query_params.get("limit", "10") or 10), 50)
    before = request.query_params.get("before") or ""

    def cursor(conv: dict) -> tuple[str, str]:
        # updated_at 只到秒：同秒会话用 id 做稳定次序，避免翻页跳行/重复
        return (conv.get("updated_at") or "", conv.get("id") or "")

    convs = db.list_conversations(tenant_id=auth.tenant_id,
                                  customer_id=auth.actor_id)
    convs.sort(key=cursor, reverse=True)
    if before:
        before_at, _, before_id = before.partition("|")
        convs = [c for c in convs if cursor(c) < (before_at, before_id)]
    page = convs[:limit]
    out = []
    for c in page:
        msgs = db.list_messages(c["id"], tenant_id=auth.tenant_id)
        out.append({
            "id": c["id"], "state": c.get("state"), "status": c.get("status"),
            "updated_at": c.get("updated_at"), "created_at": c.get("created_at"),
            "message_count": len(msgs),
            "last_message": (msgs[-1]["content"][:60] if msgs else ""),
        })
    next_before = ""
    if len(page) == limit:
        next_before = f"{page[-1]['updated_at']}|{page[-1]['id']}"
    return JSONResponse({"conversations": out, "next_before": next_before})


async def api_customer_handoff(request):  # noqa: ANN201
    """一键转人工：把当前会话上下文交给编排 handoff，无需客户重复描述。"""
    auth = auth_from_request(request)
    if auth is None or auth.role != "customer":
        return _forbidden()
    cid = request.path_params["cid"]
    try:
        conv = _conv_for(auth, cid)
    except KeyError:
        return JSONResponse({"error": "not_found"}, status_code=404)
    except PermissionError:
        return _forbidden()
    if conv.get("state") == "CLOSED" or conv.get("status") == "closed":
        return JSONResponse({"error": "conversation_closed"}, status_code=409)
    result = await handle_customer_message(cid, "转人工", auth,
                                           customer_id=auth.actor_id)
    refreshed = db.get_conversation(cid, tenant_id=auth.tenant_id)
    return JSONResponse({
        "conversation_id": cid,
        "state": (refreshed or {}).get("state"),
        "status": (refreshed or {}).get("status"),
        "reply": result.get("reply"),
    })


async def api_customer_send(request):  # noqa: ANN201
    auth = auth_from_request(request)
    if auth is None or auth.role not in ("customer", "agent", "supervisor", "admin"):
        return _forbidden()
    limited = _rate_limit(auth, request)
    if limited:
        return limited
    body = await request.json()
    text = str(body.get("text", "")).strip()[:2000]
    if not text:
        return JSONResponse({"error": "empty"}, status_code=400)
    customer_id = auth.actor_id if auth.role == "customer" else (
        str(body.get("customer_id") or "").strip() or None
    )
    cid = str(body.get("conversation_id") or "")
    conv = db.get_conversation(cid, tenant_id=auth.tenant_id) if cid else None
    if conv is None:
        cid = db.create_conversation(customer_id, tenant_id=auth.tenant_id)["id"]
    attachment_ids = body.get("attachment_ids") or []
    if not isinstance(attachment_ids, list):
        return JSONResponse({"error": "bad_attachment_ids"}, status_code=400)
    try:
        result = await handle_customer_message(
            cid, text, auth, customer_id=customer_id,
            attachment_ids=[str(a) for a in attachment_ids],
        )
    except PermissionError as exc:
        return JSONResponse({"error": str(exc)}, status_code=403)
    return JSONResponse(result)


async def api_agent_reply(request):  # noqa: ANN201
    auth = auth_from_request(request)
    if auth is None or auth.role not in ("agent", "supervisor", "admin"):
        return _forbidden()
    cid = request.path_params["cid"]
    body = await request.json()
    text = str(body.get("text", "")).strip()[:2000]
    if not text:
        return JSONResponse({"error": "empty"}, status_code=400)
    try:
        return JSONResponse(await handle_agent_reply(cid, text, auth))
    except KeyError:
        return JSONResponse({"error": "not_found"}, status_code=404)
    except PermissionError as e:
        return JSONResponse({"error": str(e)}, status_code=409)


async def api_tools(request):  # noqa: ANN201
    auth = auth_from_request(request)
    if auth is None or auth.role not in ("agent", "supervisor", "admin"):
        return _forbidden()
    return JSONResponse(console_tools.registry(auth))


async def api_tool_invoke(request):  # noqa: ANN201
    auth = auth_from_request(request)
    if auth is None or auth.role not in ("agent", "supervisor", "admin"):
        return _forbidden()
    limited = _rate_limit(auth, request)
    if limited:
        return limited
    tid = request.path_params["tid"]
    body = await request.json()
    result = await console_tools.invoke(
        tid, body.get("params") or {}, body.get("conversation_id"), auth=auth
    )
    return JSONResponse(result)


async def api_pending_actions(request):  # noqa: ANN201
    auth = auth_from_request(request)
    if auth is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    cid = request.path_params["cid"]
    try:
        _conv_for(auth, cid)
    except KeyError:
        return JSONResponse({"error": "not_found"}, status_code=404)
    except PermissionError:
        return _forbidden()
    return JSONResponse(db.list_pending_actions(cid, tenant_id=auth.tenant_id))


async def api_resume_action(request):  # noqa: ANN201
    auth = auth_from_request(request)
    if auth is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    cid = request.path_params["cid"]
    try:
        conv = _conv_for(auth, cid)
    except KeyError:
        return JSONResponse({"error": "not_found"}, status_code=404)
    except PermissionError:
        return _forbidden()
    body = await request.json()
    action_id = str(body.get("action_id") or "")
    pending = db.get_pending_action(action_id, tenant_id=auth.tenant_id)
    if not pending or pending.get("conversation_id") != cid:
        return JSONResponse({"error": "not_found"}, status_code=404)
    result = await _resume_pending_action(pending, auth, conv)
    if not result.get("ok"):
        return JSONResponse(result, status_code=int(result.pop("status", 400)))
    return JSONResponse(result)


async def api_approval_execute(request):  # noqa: ANN201
    auth = auth_from_request(request)
    if auth is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not auth.has_scope("approval.write"):
        return _forbidden()
    aid = request.path_params["aid"]
    pending = db.get_pending_action_by_approval(aid, tenant_id=auth.tenant_id)
    if not pending:
        return JSONResponse({"error": "not_found"}, status_code=404)
    conv = db.get_conversation(
        pending["conversation_id"], tenant_id=auth.tenant_id,
    )
    if conv is None:
        return JSONResponse({"error": "not_found"}, status_code=404)
    result = await _resume_pending_action(pending, auth, conv)
    if not result.get("ok"):
        return JSONResponse(result, status_code=int(result.pop("status", 400)))
    return JSONResponse(result)


async def api_attachment_upload(request):  # noqa: ANN201
    """multipart 上传：内容嗅探/大小限制/私有存储/sha256/审计。"""
    auth = auth_from_request(request)
    if auth is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    limited = _rate_limit(auth, request)
    if limited:
        return limited
    # 读前限量：声明长度超限或流式读取超限都直接 400，不进内存再判
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > attachment_store.MAX_UPLOAD_BYTES + 4096:
        return JSONResponse({"error": "file_too_large"}, status_code=400)
    form = await request.form(max_fields=10, max_files=1)
    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        return JSONResponse({"error": "missing_file"}, status_code=400)
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > attachment_store.MAX_UPLOAD_BYTES:
            return JSONResponse({"error": "file_too_large"}, status_code=400)
        chunks.append(chunk)
    data = b"".join(chunks)
    cid = str(form.get("conversation_id") or "")
    if cid:
        try:
            _conv_for(auth, cid)
        except KeyError:
            return JSONResponse({"error": "not_found"}, status_code=404)
        except PermissionError:
            return _forbidden()
    try:
        row = attachment_store.save_attachment(
            tenant_id=auth.tenant_id,
            uploader_id=auth.actor_id,
            filename=getattr(upload, "filename", "") or "attachment",
            data=data,
            conversation_id=cid,
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    audit_from_auth(
        auth, "console.attachment_upload", target=f"attachment:{row['attachment_id']}",
        params={"mime": row["mime"], "size": row["size"], "sha256": row["sha256"],
                "conversation_id": cid},
        context_id=cid,
    )
    return JSONResponse({
        "attachment_id": row["attachment_id"], "filename": row["filename"],
        "mime": row["mime"], "size": row["size"], "sha256": row["sha256"],
    })


async def api_attachment_download(request):  # noqa: ANN201
    """鉴权下载：归属校验 + 固定 Content-Type/Disposition，路径不含用户输入。"""
    auth = auth_from_request(request)
    if auth is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    attachment_id = str(request.path_params["aid"] or "")
    if not re.fullmatch(r"[0-9a-f]{32}", attachment_id):
        return JSONResponse({"error": "bad_attachment_id"}, status_code=400)
    row = db.get_attachment(attachment_id, tenant_id=auth.tenant_id)
    if row is None:
        return JSONResponse({"error": "not_found"}, status_code=404)
    if not attachment_store.can_download(
        row, auth,
        conversation_lookup=lambda c: db.get_conversation(
            c, tenant_id=auth.tenant_id),
    ):
        return _forbidden()
    path = Path(row["storage_path"])
    if not path.is_file() or not path.resolve().is_relative_to(
            attachment_store.UPLOAD_ROOT.resolve()):
        return JSONResponse({"error": "not_found"}, status_code=404)
    audit_from_auth(
        auth, "console.attachment_download", target=f"attachment:{attachment_id}",
        context_id=str(row["conversation_id"] or ""),
    )
    from urllib.parse import quote

    ascii_name = row["filename"].encode("ascii", "ignore").decode() or "attachment"
    encoded = quote(row["filename"])
    return FileResponse(
        path,
        media_type=row["mime"],
        headers={
            "Content-Disposition": (
                f"attachment; filename=\"{ascii_name}\"; "
                f"filename*=UTF-8''{encoded}"
            )
        },
    )


# ---------------------------------------------------------------------------
# 2B：工单 CRUD / 分派 / SLA（逻辑在 aftersale 域 internal_api，此处只代理）
# ---------------------------------------------------------------------------

def _ticket_auth(auth: AuthContext | None):
    if auth is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if auth.role not in ("agent", "supervisor", "admin", "finance"):
        return _forbidden()
    return None


def _proxy_response(status: int, payload: dict):  # noqa: ANN201
    """内部 API 响应透传（业务错误码原样；201 保留）。"""
    return JSONResponse(payload, status_code=status if status >= 400 else status)


async def api_ticket_create(request):  # noqa: ANN201
    """坐席建工单：逻辑在 aftersale 域 /internal/tickets，此处薄代理 + 审计。"""
    auth = auth_from_request(request)
    denied = _ticket_auth(auth)
    if denied:
        return denied
    body = await request.json()
    from shared import aftersale_client

    try:
        status, payload = aftersale_client.create_ticket(auth, body)
    except aftersale_client.AftersaleUnavailable as e:
        logger.warning("工单内部 API 不可用：%s", e)
        return JSONResponse({"error": "domain_unavailable", "detail": str(e)[:200]},
                            status_code=503)
    if status < 400:
        audit_from_auth(
            auth, "console.ticket_create", target=f"ticket:{payload.get('ticket_no')}",
            params={"customer_id": body.get("customer_id"), "sku": body.get("sku"),
                    "priority": body.get("priority"),
                    "department": body.get("department")},
            context_id=str(body.get("conversation_id") or ""),
        )
    return _proxy_response(status, payload)


async def api_ticket_list(request):  # noqa: ANN201
    auth = auth_from_request(request)
    denied = _ticket_auth(auth)
    if denied:
        return denied
    from shared import aftersale_client

    params = {
        k: v for k, v in (
            ("customer_id", request.query_params.get("customer_id") or ""),
            ("status", request.query_params.get("status") or ""),
        ) if v
    }
    try:
        status, payload = aftersale_client.list_tickets(auth, params)
    except aftersale_client.AftersaleUnavailable as e:
        logger.warning("工单内部 API 不可用：%s", e)
        return JSONResponse({"error": "domain_unavailable", "detail": str(e)[:200]},
                            status_code=503)
    return _proxy_response(status, payload)


async def api_ticket_patch(request):  # noqa: ANN201
    auth = auth_from_request(request)
    denied = _ticket_auth(auth)
    if denied:
        return denied
    ticket_no = str(request.path_params["ticket_no"] or "").upper()
    body = await request.json()
    from shared import aftersale_client

    fields: dict = {}
    for key in ("status", "priority", "sla_due", "description", "department",
                "assignee_id"):
        if key in body:
            fields[key] = str(body.get(key) or "") or None
    try:
        status, payload = aftersale_client.patch_ticket(auth, ticket_no, fields)
    except aftersale_client.AftersaleUnavailable as e:
        logger.warning("工单内部 API 不可用：%s", e)
        return JSONResponse({"error": "domain_unavailable", "detail": str(e)[:200]},
                            status_code=503)
    if status < 400:
        audit_from_auth(
            auth, "console.ticket_update", target=f"ticket:{ticket_no}",
            params={**fields}, context_id=str(body.get("conversation_id") or ""),
        )
    return _proxy_response(status, payload)


async def api_ticket_assign(request):  # noqa: ANN201
    auth = auth_from_request(request)
    denied = _ticket_auth(auth)
    if denied:
        return denied
    ticket_no = str(request.path_params["ticket_no"] or "").upper()
    body = await request.json()
    from shared import aftersale_client

    payload_body = {
        "department": body.get("department") or "",
        "assignee_id": body.get("assignee_id"),
        "priority": body.get("priority"),
        "sla_due": body.get("sla_due"),
    }
    try:
        status, payload = aftersale_client.assign_ticket(auth, ticket_no, payload_body)
    except aftersale_client.AftersaleUnavailable as e:
        logger.warning("工单内部 API 不可用：%s", e)
        return JSONResponse({"error": "domain_unavailable", "detail": str(e)[:200]},
                            status_code=503)
    if status < 400:
        audit_from_auth(
            auth, "console.ticket_assign", target=f"ticket:{ticket_no}",
            params={"department": payload_body.get("department"),
                    "assignee_id": payload_body.get("assignee_id"),
                    "priority": payload_body.get("priority")},
        )
    return _proxy_response(status, payload)


async def api_feedback(request):  # noqa: ANN201
    """坐席驳回/修改 AI 答复（驳(stats)回率数据源）。"""
    auth = auth_from_request(request)
    if auth is None or auth.role not in ("agent", "supervisor", "admin"):
        return _forbidden()
    cid = request.path_params["cid"]
    try:
        _conv_for(auth, cid)
    except KeyError:
        return JSONResponse({"error": "not_found"}, status_code=404)
    except PermissionError:
        return _forbidden()
    body = await request.json()
    message_id = int(body.get("message_id") or 0)
    reason = str(body.get("reason") or "").strip()
    if not message_id or not reason:
        return JSONResponse({"error": "missing_params"}, status_code=400)
    messages = db.list_messages(cid, tenant_id=auth.tenant_id)
    target = next((m for m in messages if m["id"] == message_id), None)
    if target is None:
        return JSONResponse({"error": "not_found", "detail": "message"},
                            status_code=404)
    row = db.create_feedback(
        cid, message_id, reason, tenant_id=auth.tenant_id,
        created_by=auth.actor_id,
        agent_answer=str(target.get("content") or "")[:500],
        edited_answer=str(body.get("edited_answer") or "")[:500],
    )
    audit_from_auth(
        auth, "console.feedback", target=f"message:{message_id}",
        params={"reason": reason[:100]}, context_id=cid,
    )
    return JSONResponse(row, status_code=201)


async def api_stats_summary(request):  # noqa: ANN201
    """统计看板（口径见 docs/统计口径.md）：全部来自真实聚合。"""
    auth = auth_from_request(request)
    if auth is None or auth.role not in ("supervisor", "admin"):
        return _forbidden()
    tenant = auth.tenant_id
    params = request.query_params
    date_from = str(params.get("from") or "2000-01-01")
    date_to = str(params.get("to") or "2999-12-31")

    convs = [c for c in db.list_conversations(tenant_id=tenant)
             if date_from <= (c.get("created_at") or "")[:10] <= date_to]
    total = len(convs)
    handoff_events = [
        e for e in list_events(tenant, limit=500, action="conversation.state")
        if (e.get("params") or {}).get("state") == "PENDING_HANDOFF"
        and date_from <= (e.get("created_at") or "")[:10] <= date_to
    ]
    handoff_convs = {e.get("target") for e in handoff_events}
    handoff = len(handoff_convs)
    human_events = [
        e for e in list_events(tenant, limit=500, action="conversation.state")
        if (e.get("params") or {}).get("state") == "HUMAN_ACTIVE"
        and date_from <= (e.get("created_at") or "")[:10] <= date_to
    ]
    human_handled = len({e.get("target") for e in human_events})
    closed = [c for c in convs if c.get("state") == "CLOSED"]
    durations = []
    for c in closed:
        try:
            from datetime import datetime

            started = datetime.fromisoformat(c["created_at"])
            ended = datetime.fromisoformat(c.get("closed_at") or c["updated_at"])
            durations.append((ended - started).total_seconds())
        except (ValueError, KeyError, TypeError):
            continue
    runs = [r for c in convs
            for r in db.list_agent_runs(c["id"], tenant_id=tenant, limit=50)]
    intents: dict[str, int] = {}
    for run in runs:
        intents[run.get("intent") or "unknown"] = \
            intents.get(run.get("intent") or "unknown", 0) + 1
    ai_messages = sum(
        1 for c in convs
        for m in db.list_messages(c["id"], tenant_id=tenant)
        if m.get("role") == "ai"
    )
    import sqlite3 as _sq

    conn: _sq.Connection = db._get()
    with db._lock:
        fb_rows = conn.execute(
            "SELECT conversation_id, COUNT(*) AS n FROM feedback "
            "WHERE tenant_id=? GROUP BY conversation_id",
            (tenant,),
        ).fetchall()
    fb_by_conv = {r["conversation_id"]: r["n"] for r in fb_rows}
    feedback_total = sum(fb_by_conv.get(c["id"], 0) for c in convs)
    avg_duration = round(sum(durations) / len(durations), 1) if durations else 0.0
    return JSONResponse({
        "total_conversations": total,
        "handoff_conversations": handoff,
        "ai_handled_rate": round((total - handoff) / total, 4) if total else 0.0,
        "handoff_rate": round(handoff / total, 4) if total else 0.0,
        "avg_closed_duration_seconds": avg_duration,
        "closed_conversations": len(closed),
        "intent_distribution": intents,
        "ai_messages": ai_messages,
        "feedback_count": feedback_total,
        "feedback_rate": round(feedback_total / ai_messages, 4) if ai_messages else 0.0,
        # 看板口径：驳回率与人工承接量（与 feedback_rate 同源）
        "rejection_rate": round(feedback_total / ai_messages, 4) if ai_messages else 0.0,
        "human_handled_conversations": human_handled,
    })


def _cards_from_artifacts(data: dict) -> list[dict]:
    """从域 artifact 归一化四类卡片；按钮 action_id 走白名单 registry。"""
    cards: list[dict] = []
    for domain, artifact in (data.get("artifacts") or {}).items():
        if not isinstance(artifact, dict):
            continue
        d = artifact.get("data")
        if not isinstance(d, dict) or d.get("error"):
            continue
        skill = str(artifact.get("skill") or "")
        if domain == "order" and d.get("order_no"):
            cards.append({
                "type": "order",
                "title": f"订单 {d['order_no']}",
                "fields": [
                    {"label": "状态", "value": d.get("status_name") or d.get("status")},
                    {"label": "金额", "value": d.get("amount")},
                    {"label": "SKU", "value": d.get("sku")},
                    {"label": "下单时间", "value": d.get("created_at")},
                ],
                "actions": [{"action_id": "order.track", "label": "查物流",
                             "params": {"order_no": d["order_no"]}},
                            {"action_id": "aftersale.eligibility",
                             "label": "售后资格",
                             "params": {"sku": d.get("sku", ""),
                                        "order_no": d["order_no"]}}],
            })
        elif skill == "fulfill.atp":
            cards.append({
                "type": "inventory",
                "title": f"库存 {d.get('sku', '')}",
                "fields": [
                    {"label": "可售", "value": d.get("available")},
                    {"label": "预计发货", "value": d.get("hint")},
                ],
                "actions": [{"action_id": "inventory.query", "label": "刷新库存",
                             "params": {"sku": d.get("sku", "")}}],
            })
        elif skill == "credit.query":
            cards.append({
                "type": "credit",
                "title": "授信与账期",
                "fields": [
                    {"label": "授信额度", "value": d.get("credit_limit")},
                    {"label": "已用额度", "value": d.get("credit_used")},
                    {"label": "剩余额度", "value": d.get("credit_remaining")},
                    {"label": "账期天数", "value": d.get("credit_days")},
                    {"label": "到期日", "value": d.get("credit_due_date")},
                ],
                "actions": [{"action_id": "reconciliation.view", "label": "查对账",
                             "params": {}}],
            })
        elif skill == "aftersale.eligibility" and "eligible" in d:
            cards.append({
                "type": "aftersale",
                "title": f"售后资格 {d.get('sku', '')}",
                "fields": [
                    {"label": "结论", "value": d.get("conclusion")},
                    {"label": "购买日期", "value": d.get("buy_date")},
                    {"label": "依据", "value": d.get("clause")},
                ],
                "actions": [{"action_id": "aftersale.apply", "label": "发起售后",
                             "params": {"sku": d.get("sku", ""),
                                        "fault_type": "故障",
                                        "order_no": d.get("order_no", "")}}],
            })
    return cards


async def api_card_action(request):  # noqa: ANN201
    """卡片按钮：action_id 白名单 + 签名 action token（复用 1B 机制）。"""
    auth = auth_from_request(request)
    if auth is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    cid = request.path_params["cid"]
    try:
        conv = _conv_for(auth, cid)
    except KeyError:
        return JSONResponse({"error": "not_found"}, status_code=404)
    except PermissionError:
        return _forbidden()
    if conv.get("state") == "CLOSED" or conv.get("status") == "closed":
        return JSONResponse({"error": "conversation_closed"}, status_code=409)
    body = await request.json()
    action_id = str(body.get("action_id") or "")
    params = body.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return JSONResponse({"error": "bad_params"}, status_code=400)
    resolved = resolve_action(action_id, params)
    if resolved is None:
        return JSONResponse({"error": "unknown_action", "action_id": action_id},
                            status_code=400)
    intent, slots = resolved
    if auth.role == "customer":
        customer_id = auth.actor_id
    else:
        customer_id = str(conv.get("customer_id") or "")
        if not customer_id:
            return JSONResponse({"error": "no_customer_bound"}, status_code=400)
    action_token = create_token(
        tenant_id=auth.tenant_id, actor_id=auth.actor_id, role=auth.role,
        scopes=sorted(auth.scopes), ttl_seconds=120,
        audience="orchestrator", token_type="action",
        extra_claims={"conversation_id": cid, "action_id": action_id,
                      "intent": intent, "slots": slots},
    )
    meta = {
        "trace_id": new_trace_id(), "context_id": cid, "conversation_id": cid,
        "customer_id": customer_id, "tenant_id": auth.tenant_id,
        "actor_id": auth.actor_id, "action_token": action_token,
    }
    s = get_settings()
    result = await delegate(s.domain_url("orchestrator"), action_id,
                            context_id=cid, metadata=meta, auth=auth)
    if result.state != TaskState.TASK_STATE_COMPLETED:
        return JSONResponse({"error": TaskState.Name(result.state)}, status_code=502)
    data = result.data_parts[0] if result.data_parts else {}
    reply = result.text or (data.get("final_text") if isinstance(data, dict) else "")
    cards = _cards_from_artifacts(data) if isinstance(data, dict) else []
    pending2 = _pending_from_artifacts(data, cid, auth, meta["trace_id"])
    run = None
    if isinstance(data, dict) and data.get("type") == "final_answer":
        run = _persist_agent_run(cid, auth, data, meta["trace_id"], "")
        if str(data.get("intent") or "") in REVIEW_INTENTS and \
                conv.get("state") == "AI_ACTIVE":
            await _set_conv_state(cid, "PENDING_REVIEW", auth,
                                  note=f"auto_review:{intent}")
    m = db.add_message(
        cid, "ai", reply,
        meta={"cards": cards, "run_id": (run or {}).get("run_id"),
              "pending_actions": pending2} if (cards or run or pending2) else None,
        tenant_id=auth.tenant_id,
    )
    await manager.broadcast(cid, _evt(cid, m, tenant=auth.tenant_id))
    return JSONResponse({"reply": reply, "cards": cards})


async def api_knowledge_search(request):  # noqa: ANN201
    """坐席知识库检索：逻辑在 policy 域 /internal/knowledge，此处代理 + 审计。"""
    auth = auth_from_request(request)
    if auth is None or auth.role not in ("agent", "supervisor", "admin", "finance"):
        return _forbidden()
    q = request.query_params.get("q") or ""
    status = request.query_params.get("status") or "published"
    from shared.policy_client import client as policy_client

    try:
        code, payload = policy_client.knowledge_search(auth, q=q.strip(),
                                                       status=status)
    except policy_client.PolicyUnavailable as e:
        logger.warning("知识库内部 API 不可用：%s", e)
        return JSONResponse({"error": "domain_unavailable", "detail": str(e)[:200]},
                            status_code=503)
    if code < 400:
        audit_from_auth(auth, "console.knowledge_search",
                        params={"q": q[:50], "status": status})
    return _proxy_response(code, payload)


async def api_knowledge_upsert(request):  # noqa: ANN201
    """坐席补充/修正知识：版本化写在 policy 域（发布权限域内校验），此处代理。"""
    auth = auth_from_request(request)
    if auth is None or auth.role not in ("agent", "supervisor", "admin"):
        return _forbidden()
    body = await request.json()
    from shared.policy_client import client as policy_client

    try:
        code, payload = policy_client.knowledge_upsert(auth, body)
    except policy_client.PolicyUnavailable as e:
        logger.warning("知识库内部 API 不可用：%s", e)
        return JSONResponse({"error": "domain_unavailable", "detail": str(e)[:200]},
                            status_code=503)
    if code < 400:
        audit_from_auth(
            auth, "console.knowledge_upsert",
            target=f"knowledge:{payload.get('entry_id')}@{payload.get('version')}",
            params={"status": payload.get("status"),
                    "title": str(body.get("title") or "")[:60]},
        )
    return _proxy_response(code, payload)


async def api_knowledge_versions(request):  # noqa: ANN201
    auth = auth_from_request(request)
    if auth is None or auth.role not in ("agent", "supervisor", "admin", "finance"):
        return _forbidden()
    entry_id = str(request.path_params["entry_id"] or "").strip().upper()
    from shared.policy_client import client as policy_client

    try:
        code, payload = policy_client.knowledge_versions(auth, entry_id)
    except policy_client.PolicyUnavailable as e:
        logger.warning("知识库内部 API 不可用：%s", e)
        return JSONResponse({"error": "domain_unavailable", "detail": str(e)[:200]},
                            status_code=503)
    return _proxy_response(code, payload)


# ---------------------------------------------------------------------------
# 政策发布工作流：draft → submitted → approved → published（四眼原则）
# ---------------------------------------------------------------------------

_POLICY_READ_ROLES = ("agent", "supervisor", "admin")
_POLICY_WRITE_ROLES = ("supervisor", "admin")


def _policy_read_denied(auth):  # noqa: ANN001, ANN201
    if auth is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if auth.role not in _POLICY_READ_ROLES:
        return _forbidden()
    return None


def _policy_write_denied(auth):  # noqa: ANN001, ANN201
    if auth is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if auth.role not in _POLICY_WRITE_ROLES or not auth.has_scope("policy.write"):
        return JSONResponse(
            {"error": "supervisor_required",
             "detail": "政策起草/审批/发布需主管或管理员（policy.write）"},
            status_code=403,
        )
    return None


def _policy_err(exc) -> JSONResponse:  # noqa: ANN001
    return JSONResponse({"error": exc.code, "detail": exc.detail},
                        status_code=exc.status)


async def api_policy_drafts(request):  # noqa: ANN201
    auth = auth_from_request(request)
    denied = _policy_read_denied(auth)
    if denied:
        return denied
    status = request.query_params.get("status") or None
    clause_id = request.query_params.get("clause_id") or ""
    rows = policy_publishing.list_drafts(auth.tenant_id, status, clause_id)
    audit_from_auth(auth, "console.policy_drafts_list",
                    params={"status": status or "all",
                            "clause_id": clause_id})
    return JSONResponse({"drafts": rows})


async def api_policy_draft_create(request):  # noqa: ANN201
    auth = auth_from_request(request)
    denied = _policy_write_denied(auth)
    if denied:
        return denied
    body = await request.json()
    try:
        draft = policy_publishing.create_draft(
            auth.tenant_id, auth.actor_id, body)
    except policy_publishing.PolicyError as exc:
        return _policy_err(exc)
    audit_from_auth(
        auth, "console.policy_draft_created",
        target=f"policy:{draft['clause_id']}@{draft['draft_id']}",
        params={"draft_id": draft["draft_id"],
                "clause_id": draft["clause_id"],
                "effective_from": draft["effective_from"]},
    )
    return JSONResponse({"draft": draft}, status_code=201)


async def api_policy_draft_detail(request):  # noqa: ANN201
    auth = auth_from_request(request)
    denied = _policy_read_denied(auth)
    if denied:
        return denied
    draft_id = str(request.path_params["draft_id"] or "").strip().upper()
    draft = policy_publishing.get_draft(auth.tenant_id, draft_id)
    if draft is None:
        return JSONResponse({"error": "not_found"}, status_code=404)
    diff = policy_publishing.diff_draft(auth.tenant_id, draft_id)
    return JSONResponse({"draft": draft, "diff": diff})


async def api_policy_draft_action(request):  # noqa: ANN201
    auth = auth_from_request(request)
    denied = _policy_write_denied(auth)
    if denied:
        return denied
    action = str(request.path_params["action"] or "").strip()
    draft_id = str(request.path_params["draft_id"] or "").strip().upper()
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    try:
        if action == "submit":
            draft = policy_publishing.submit_draft(
                auth.tenant_id, draft_id, auth.actor_id)
            audit_action = "console.policy_draft_submitted"
            params = {"draft_id": draft_id, "clause_id": draft["clause_id"]}
        elif action == "approve":
            draft = policy_publishing.approve_draft(
                auth.tenant_id, draft_id, auth.actor_id)
            audit_action = "console.policy_draft_approved"
            params = {"draft_id": draft_id, "clause_id": draft["clause_id"]}
        elif action == "reject":
            draft = policy_publishing.reject_draft(
                auth.tenant_id, draft_id, auth.actor_id,
                str(body.get("note") or ""))
            audit_action = "console.policy_draft_rejected"
            params = {"draft_id": draft_id, "clause_id": draft["clause_id"],
                      "note": str(body.get("note") or "")[:100]}
        elif action == "publish":
            draft = policy_publishing.publish_draft(
                auth.tenant_id, draft_id, auth.actor_id)
            audit_action = "console.policy_draft_published"
            params = {"draft_id": draft_id, "clause_id": draft["clause_id"],
                      "version": draft.get("version"),
                      "effective_from": draft["effective_from"]}
        else:
            return JSONResponse({"error": "unknown_action"}, status_code=400)
    except policy_publishing.PolicyError as exc:
        return _policy_err(exc)
    audit_from_auth(
        auth, audit_action,
        target=f"policy:{draft['clause_id']}@{draft.get('version') or draft_id}",
        params=params,
    )
    return JSONResponse({"draft": draft})


async def api_policy_document_import(request):  # noqa: ANN201
    """上传 Markdown/TXT → 自动切分/关键词 → 生成多条政策草稿（仍需四眼审批）。"""
    auth = auth_from_request(request)
    denied = _policy_write_denied(auth)
    if denied:
        return denied
    form = await request.form()
    upload = form.get("file")
    if upload is None or not getattr(upload, "filename", ""):
        return JSONResponse({"error": "missing_file"}, status_code=400)
    raw = await upload.read()
    effective_from = str(form.get("effective_from") or "")
    try:
        result = policy_ingestion.import_document(
            auth.tenant_id, auth.actor_id, upload.filename, raw,
            effective_from=effective_from)
    except policy_ingestion.IngestionError as exc:
        return JSONResponse(
            {"error": exc.code, "detail": exc.detail, "line": exc.line},
            status_code=exc.status)
    audit_from_auth(
        auth, "console.policy_document_uploaded",
        target=f"policy_doc:{result['doc_id']}",
        params={"filename": result["filename"], "sha256": result["sha256"],
                "clauses": result["clause_count"]},
    )
    return JSONResponse(result, status_code=201)


def _staff_or_403(auth):  # noqa: ANN001, ANN201
    if auth is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if auth.role not in ("agent", "supervisor", "admin", "finance"):
        return _forbidden()
    return None


async def api_customer360(request):  # noqa: ANN201
    """客户 360 只读聚合：业务数据经 customer 域（customer.overview），
    本端只合并会话绑定与坐席已查询步骤（console.db）。"""
    auth = auth_from_request(request)
    denied = _staff_or_403(auth)
    if denied:
        return denied
    cid = request.path_params["cid"]
    try:
        conv = _conv_for(auth, cid)
    except KeyError:
        return JSONResponse({"error": "not_found"}, status_code=404)
    except PermissionError:
        return _forbidden()
    customer_id = str(conv.get("customer_id") or "")
    if not customer_id:
        return JSONResponse({"error": "no_customer_bound"}, status_code=400)
    tenant = auth.tenant_id

    try:
        result = await delegate(
            get_settings().domain_url("customer"),
            f"客户360：{customer_id}",
            metadata={
                "trace_id": f"console-360-{cid}",
                "context_id": cid,
                "customer_id": customer_id,
            },
            auth=auth,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("customer360 委托失败：%s", e)
        return JSONResponse(
            {"error": "domain_unavailable", "detail": str(e)[:200]}, status_code=503,
        )
    data = result.data_parts[0] if result.data_parts else {}
    if result.state != TaskState.TASK_STATE_COMPLETED or not isinstance(data, dict):
        return JSONResponse(
            {"error": "overview_unavailable", "state": str(result.state)},
            status_code=502,
        )
    if data.get("error"):
        return JSONResponse({"error": data["error"]}, status_code=404)
    # delegate 的 data_parts[0] 是完整 Artifact 信封（authority_domain/tool_events
    # + data）；业务聚合在信封的 data 字段里，拆包后合并给前端。
    if isinstance(data.get("data"), dict):
        data = data["data"]
        if data.get("error"):
            return JSONResponse({"error": data["error"]}, status_code=404)

    payload = {
        "conversation_id": cid,
        **data,
        "agent_queries": [
            {
                "stage": st.get("stage"), "domain": st.get("domain"),
                "tool": st.get("tool"), "output_summary": st.get("output_summary"),
                "status": st.get("status"), "created_at": st.get("created_at"),
                "trace_id": run.get("trace_id"),
            }
            for run in db.list_agent_runs(cid, tenant_id=tenant, limit=1)
            for st in run.get("steps", [])
            if st.get("stage") in ("domain_call", "tool")
        ][:20],
    }
    audit_from_auth(
        auth, "console.customer360", target=f"customer:{customer_id}",
        params={"conversation_id": cid}, context_id=cid,
    )
    return JSONResponse(payload)


async def api_trace(request):  # noqa: ANN201
    """会话级 Agent 调用链：agent_runs + steps（staff only）。"""
    auth = auth_from_request(request)
    denied = _staff_or_403(auth)
    if denied:
        return denied
    cid = request.path_params["cid"]
    try:
        _conv_for(auth, cid)
    except KeyError:
        return JSONResponse({"error": "not_found"}, status_code=404)
    except PermissionError:
        return _forbidden()
    runs = db.list_agent_runs(cid, tenant_id=auth.tenant_id)
    audit_from_auth(
        auth, "console.trace", target=cid,
        params={"runs": len(runs)}, context_id=cid,
    )
    return JSONResponse({"runs": runs})


async def api_trace_spans(request):  # noqa: ANN201
    """按 trace_id 查跨进程 span 树（M4 / PRD 7.10，staff only）。

    数据源：trace_dir 下各服务的 *.jsonl（FileSpanExporter 落盘），
    重建为以入口（console 渠道 span）为根的调用树；与审计/agent_runs 同 trace_id。
    """
    auth = auth_from_request(request)
    denied = _staff_or_403(auth)
    if denied:
        return denied
    trace_id = request.path_params["trace_id"]
    spans = read_trace_spans(trace_id)
    audit_from_auth(
        auth, "console.trace_spans", target=trace_id,
        params={"spans": len(spans)},
    )
    if not spans:
        return JSONResponse(
            {"error": "not_found", "trace_id": trace_id}, status_code=404
        )
    return JSONResponse({
        "trace_id": trace_id,
        "count": len(spans),
        "services": sorted({s.get("service", "") for s in spans}),
        "tree": build_span_tree(spans),
    })


async def api_close(request):  # noqa: ANN201
    """结束会话：客户直接 CLOSED；坐席/主管转 PENDING_REVIEW 待复核。"""
    auth = auth_from_request(request)
    if auth is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    cid = request.path_params["cid"]
    try:
        conv = _conv_for(auth, cid)
    except KeyError:
        return JSONResponse({"error": "not_found"}, status_code=404)
    except PermissionError:
        return _forbidden()
    if conv.get("state") == "CLOSED" or conv.get("status") == "closed":
        return JSONResponse({"error": "already_closed"}, status_code=409)
    if auth.role == "customer":
        await _set_conv_state(cid, "CLOSED", auth, note="customer_close")
    elif auth.role in ("agent", "supervisor", "admin"):
        await _set_conv_state(cid, "PENDING_REVIEW", auth, note="staff_close")
    else:
        return _forbidden()
    msg = db.add_message(cid, "system", "会话已结束。", tenant_id=auth.tenant_id)
    await manager.broadcast(cid, _evt(cid, msg, tenant=auth.tenant_id))
    return JSONResponse(db.get_conversation(cid, tenant_id=auth.tenant_id))


async def api_review(request):  # noqa: ANN201
    """主管复核 PENDING_REVIEW：approve → CLOSED；reject → HUMAN_ACTIVE。"""
    auth = auth_from_request(request)
    if auth is None or auth.role not in ("supervisor", "admin"):
        return _forbidden()
    cid = request.path_params["cid"]
    body = await request.json()
    decision = str(body.get("decision") or "")
    if decision not in ("approve", "reject"):
        return JSONResponse({"error": "bad_decision"}, status_code=400)
    conv = db.get_conversation(cid, tenant_id=auth.tenant_id)
    if conv is None:
        return JSONResponse({"error": "not_found"}, status_code=404)
    if (conv.get("state") not in ("PENDING_REVIEW",)
            and conv.get("status") != "review"):
        return JSONResponse(
            {"error": f"状态 {conv.get('state')} 不可复核"}, status_code=409)
    note = f"review_{decision}"
    if decision == "approve":
        await _set_conv_state(cid, "CLOSED", auth, note=note)
    else:
        await _set_conv_state(cid, "HUMAN_ACTIVE", auth, note=note)
    return JSONResponse(db.get_conversation(cid, tenant_id=auth.tenant_id))


async def api_audit(request):  # noqa: ANN201
    auth = auth_from_request(request)
    if auth is None or not auth.has_scope("audit.read"):
        return _forbidden()
    cid = request.query_params.get("conversation_id") or ""
    action = request.query_params.get("action") or ""
    trace_id = request.query_params.get("trace_id") or ""
    return JSONResponse(list_events(
        auth.tenant_id, limit=50, context_id=cid, action=action, trace_id=trace_id,
    ))


async def api_audit_verify(request):  # noqa: ANN201
    auth = auth_from_request(request)
    if auth is None or not auth.has_scope("audit.read"):
        return _forbidden()
    return JSONResponse(verify_chain(auth.tenant_id))


async def api_audit_events(request):  # noqa: ANN201
    auth = auth_from_request(request)
    if auth is None or not auth.has_scope("audit.read"):
        return _forbidden()
    return JSONResponse(list_events(auth.tenant_id))


async def api_audit_events_verify(request):  # noqa: ANN201
    auth = auth_from_request(request)
    if auth is None or not auth.has_scope("audit.read"):
        return _forbidden()
    return JSONResponse(verify_chain(auth.tenant_id))


# ---------------------------------------------------------------------------
# 审批（高价值动作 + 双人复核）
# ---------------------------------------------------------------------------

async def api_approvals(request):  # noqa: ANN201
    auth = auth_from_request(request)
    if auth is None or not auth.has_scope("approval.read"):
        return _forbidden()
    status = request.query_params.get("status")
    return JSONResponse(approvals.list_approvals(auth.tenant_id, status=status))


async def api_approve(request):  # noqa: ANN201
    auth = auth_from_request(request)
    if auth is None or not auth.has_scope("approval.write"):
        return _forbidden()
    approval_id = str(request.path_params["aid"])
    result = approvals.approve(
        auth.tenant_id, approval_id, actor_id=auth.actor_id, role=auth.role
    )
    if result.get("error"):
        return JSONResponse(result, status_code=409)
    return JSONResponse(result)


async def api_reject(request):  # noqa: ANN201
    auth = auth_from_request(request)
    if auth is None or not auth.has_scope("approval.write"):
        return _forbidden()
    approval_id = str(request.path_params["aid"])
    body = {}
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        pass
    result = approvals.reject(
        auth.tenant_id, approval_id, actor_id=auth.actor_id, role=auth.role,
        reason=str(body.get("reason", "")),
    )
    if result.get("error"):
        return JSONResponse(result, status_code=409)
    return JSONResponse(result)


# ---------------------------------------------------------------------------
# WebSocket：首包认证 + 双向聊天
# ---------------------------------------------------------------------------

def _allowed_origins() -> set[str]:
    s = get_settings()
    if s.allowed_origins.strip():
        return {x.strip() for x in s.allowed_origins.split(",") if x.strip()}
    port = s.console_port
    return {
        f"http://127.0.0.1:{port}", f"http://localhost:{port}",
        f"https://127.0.0.1:{port}", f"https://localhost:{port}",
    }


def _origin_ok(origin: str) -> bool:
    if not origin:
        return get_settings().env == "test"
    return origin in _allowed_origins()


async def ws_chat(websocket: WebSocket) -> None:
    cid = websocket.path_params["cid"]
    if not _origin_ok(websocket.headers.get("origin", "")):
        audit_event(
            tenant_id="unknown", actor_id="anonymous", actor_role="anonymous",
            action="ws.auth_failed", target=cid, result_summary="bad origin",
        )
        await websocket.accept()
        await websocket.close(code=1008)
        return
    await websocket.accept()
    # 浏览器同源 Cookie 认证（推荐）；无 Cookie 时兼容首包 token（测试/非浏览器）。
    surface = websocket.query_params.get("surface", "")
    auth, auth_surface, _ = auth_from_cookie_values(
        dict(websocket.cookies), surface=surface,
    )
    auth_source = "cookie" if auth is not None else ""
    if auth is None:
        try:
            first = await asyncio.wait_for(websocket.receive_json(), timeout=10)
        except Exception:  # noqa: BLE001
            audit_event(
                tenant_id="unknown", actor_id="anonymous", actor_role="anonymous",
                action="ws.auth_failed", target=cid, result_summary="auth timeout",
            )
            await websocket.close(code=1008)
            return
        if first.get("type") != "auth" or not first.get("token"):
            audit_event(
                tenant_id="unknown", actor_id="anonymous", actor_role="anonymous",
                action="ws.auth_failed", target=cid, result_summary="missing first auth packet",
            )
            await websocket.close(code=1008)
            return
        try:
            auth = decode_token(str(first["token"]))
            auth_source = "first_packet"
        except AuthError:
            audit_event(
                tenant_id="unknown", actor_id="anonymous", actor_role="anonymous",
                action="ws.auth_failed", target=cid, result_summary="bad token",
            )
            await websocket.close(code=1008)
            return
    if auth.role not in ("customer", "agent", "supervisor", "admin"):
        audit_event(
            tenant_id=auth.tenant_id, actor_id=auth.actor_id, actor_role=auth.role,
            action="ws.auth_failed", target=cid, result_summary="role not allowed",
        )
        await websocket.close(code=1008)
        return
    try:
        conv = _conv_for(auth, cid)
    except (KeyError, PermissionError):
        audit_event(
            tenant_id=auth.tenant_id, actor_id=auth.actor_id, actor_role=auth.role,
            action="ws.auth_failed", target=cid, result_summary="conversation denied",
        )
        await websocket.close(code=1008)
        return
    if auth.role == "customer" and conv.get("customer_id") and \
            conv["customer_id"] != auth.actor_id:
        audit_event(
            tenant_id=auth.tenant_id, actor_id=auth.actor_id, actor_role=auth.role,
            action="ws.auth_failed", target=cid, result_summary="not your conversation",
        )
        await websocket.close(code=1008)
        return
    audit_event(
        tenant_id=auth.tenant_id, actor_id=auth.actor_id, actor_role=auth.role,
        action="ws.auth", target=cid, context_id=cid,
        params={"source": auth_source, "surface": auth_surface},
    )
    manager.add(cid, websocket)
    try:
        await websocket.send_json({
            "type": "auth_ok",
            "role": auth.role,
            "user": {"tenant_id": auth.tenant_id, "user_id": auth.actor_id},
        })
        await websocket.send_json({
            "type": "snapshot",
            "messages": db.list_messages(cid, tenant_id=auth.tenant_id),
            "conversation": db.get_conversation(cid, tenant_id=auth.tenant_id),
            "role": auth.role,
        })
        while True:
            data = await websocket.receive_json()
            ok, retry = ratelimit.check_user(auth.tenant_id, auth.actor_id, "ws")
            if not ok:
                await websocket.send_json(
                    {"type": "error", "detail": "rate_limited", "retry_after": retry}
                )
                continue
            text = str(data.get("text", "")).strip()[:2000]
            if not text:
                continue
            if auth.role == "agent":
                try:
                    await handle_agent_reply(cid, text, auth)
                except (PermissionError, KeyError) as e:
                    await websocket.send_json({"type": "error", "detail": str(e)})
            elif auth.role in ("supervisor", "admin"):
                # 主管/管理员只读观察，不直接代表坐席发言（避免越权）
                await websocket.send_json({"type": "error", "detail": "use workbench reply"})
            else:
                conv = db.get_conversation(cid, tenant_id=auth.tenant_id)
                if conv and conv["mode"] == "human":
                    await handle_customer_message(cid, text, auth)
                else:
                    await websocket.send_json({"type": "status", "detail": "AI 处理中…"})
                    await handle_customer_message(cid, text, auth)
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        logger.exception("ws 异常 conv=%s", cid)
    finally:
        manager.disconnect(cid, websocket)


# ---------------------------------------------------------------------------
# 坐席 AI 助手（toB）：按租户+坐席隔离会话
# ---------------------------------------------------------------------------

async def api_assist_ask(request):  # noqa: ANN201
    auth = auth_from_request(request)
    if auth is None or auth.role not in ("agent", "supervisor", "admin"):
        return _forbidden()
    limited = _rate_limit(auth, request)
    if limited:
        return limited
    body = await request.json()
    question = str(body.get("question", "")).strip()[:2000]
    if not question:
        return JSONResponse({"error": "empty"}, status_code=400)
    question, _ = mask_pii(question)
    task_id = body.get("task_id") or None
    conv_id = str(body.get("conversation_id") or "")
    conv = db.get_conversation(conv_id, tenant_id=auth.tenant_id) if conv_id else None
    customer_id = (conv or {}).get("customer_id") or ""
    db.add_assist_message(
        "agent", question, tenant_id=auth.tenant_id, agent_id=auth.actor_id
    )

    s = get_settings()
    orch_url = s.domain_url("orchestrator")
    context_id = f"agent-assist:{auth.tenant_id}:{auth.actor_id}"
    result = await delegate(
        orch_url, question,
        task_id=task_id, context_id=context_id,
        metadata={
            "trace_id": new_trace_id(),
            "context_id": context_id,
            "customer_id": customer_id,
            "hit_wall_count": "0",
            "tenant_id": auth.tenant_id,
            "actor_id": auth.actor_id,
        },
        auth=auth,
    )
    if result.state == TaskState.TASK_STATE_INPUT_REQUIRED:
        payload = result.status_data or {}
        q = str(payload.get("question", "请补充一下您的需求。"))
        db.add_assist_message("ai", q, tenant_id=auth.tenant_id, agent_id=auth.actor_id)
        return JSONResponse({"type": "clarify", "reply": q, "task_id": result.task_id})
    data = result.data_parts[0] if result.data_parts else {}
    reply = result.text or (
        data.get("final_text") if isinstance(data, dict) else ""
    ) or "（AI 暂未返回内容，请重试）"
    db.add_assist_message("ai", reply, tenant_id=auth.tenant_id, agent_id=auth.actor_id)
    return JSONResponse({"type": "ai", "reply": reply, "task_id": None})


async def api_assist_history(request):  # noqa: ANN201
    auth = auth_from_request(request)
    if auth is None or auth.role not in ("agent", "supervisor", "admin"):
        return _forbidden()
    return JSONResponse(db.list_assist(tenant_id=auth.tenant_id))


# ---------------------------------------------------------------------------
# Phase 4：内部坐席工作台（队列 / 摘要 / 备注 / 驳回 / 转交 / 快捷回复）
# ---------------------------------------------------------------------------

WORKBENCH_ROLES = ("agent", "supervisor", "admin")
QUEUE_TAB_STATES = {
    "pending": {"PENDING_HANDOFF"},
    "review": {"PENDING_REVIEW"},
    "human": {"HUMAN_ACTIVE"},
    "closed": {"CLOSED"},
}
SLA_SECONDS = 600


def _workbench_or_403(auth: AuthContext | None):
    if auth is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if auth.role not in WORKBENCH_ROLES:
        return _forbidden()
    return None


def _waiting_seconds(iso: str) -> int:
    try:
        started = datetime.fromisoformat(iso)
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        return max(0, int((datetime.now(timezone.utc) - started).total_seconds()))
    except (ValueError, TypeError):
        return 0


def _business_customer_maps(tenant: str) -> tuple[dict, dict]:
    from shared.db import connect_business_db

    customers: dict[str, dict] = {}
    profiles: dict[str, dict] = {}
    try:
        with connect_business_db() as conn:
            for row in conn.execute(
                "SELECT customer_id, name, tier, phone, city FROM customers "
                "WHERE tenant_id=?", (tenant,),
            ):
                customers[row["customer_id"]] = dict(row)
            for row in conn.execute(
                "SELECT * FROM customer_profiles WHERE tenant_id=?", (tenant,),
            ):
                profiles[row["customer_id"]] = dict(row)
    except Exception:  # noqa: BLE001
        logger.exception("customer maps 查询失败 tenant=%s", tenant)
    return customers, profiles


def _queue_priority(profile: dict) -> str:
    if not profile:
        return "normal"
    if profile.get("priority") == "high" or profile.get("risk_level") == "high":
        return "high"
    if profile.get("risk_level") == "medium":
        return "medium"
    return "normal"


def _priority_rank(value: str) -> int:
    return {"high": 3, "medium": 2, "normal": 1}.get(value, 1)


def _handoff_reasons(tenant: str) -> dict[str, str]:
    rows = db._query(
        "SELECT conversation_id, reason FROM handoff_queue "
        "WHERE tenant_id=? ORDER BY id", (tenant,),
    )
    return {r["conversation_id"]: r["reason"] or "" for r in rows}


async def api_workbench_queues(request):  # noqa: ANN201
    """队列 tab：待接入 / Agent 待复核 / 人工中 / 已结束（含未读、SLA、优先级）。"""
    auth = auth_from_request(request)
    denied = _workbench_or_403(auth)
    if denied:
        return denied
    tab = request.query_params.get("tab") or "pending"
    if tab not in QUEUE_TAB_STATES:
        return JSONResponse({"error": "bad_tab"}, status_code=400)
    priority = request.query_params.get("priority") or ""
    if priority and priority not in ("high", "medium", "normal"):
        return JSONResponse({"error": "bad_priority"}, status_code=400)
    query = (request.query_params.get("q") or "").strip().casefold()
    sort = request.query_params.get("sort") or "waiting"
    if sort not in ("waiting", "priority", "recent"):
        return JSONResponse({"error": "bad_sort"}, status_code=400)
    try:
        cursor = int(request.query_params.get("cursor") or 0)
    except ValueError:
        return JSONResponse({"error": "bad_cursor"}, status_code=400)
    if cursor < 0:
        return JSONResponse({"error": "bad_cursor"}, status_code=400)
    try:
        limit = min(max(1, int(request.query_params.get("limit") or 20)), 100)
    except ValueError:
        return JSONResponse({"error": "bad_limit"}, status_code=400)
    if "limit" in request.query_params and \
            (request.query_params.get("limit") or "").strip() in ("", "0", "-1"):
        return JSONResponse({"error": "bad_limit"}, status_code=400)

    if auth.role == "agent":
        convs = db.list_conversations(
            tenant_id=auth.tenant_id, assigned_agent_id=auth.actor_id, limit=400,
        )
    else:
        convs = db.list_conversations(tenant_id=auth.tenant_id, limit=500)

    customers, profiles = _business_customer_maps(auth.tenant_id)
    agents = {u["user_id"]: u.get("display_name") or u["user_id"]
              for u in db.list_users(tenant_id=auth.tenant_id,
                                     roles=("agent", "supervisor", "admin"))}
    reasons = _handoff_reasons(auth.tenant_id)

    counts = {name: 0 for name in QUEUE_TAB_STATES}
    enriched = []
    for conv in convs:
        state = str(conv.get("state") or "")
        for name, states in QUEUE_TAB_STATES.items():
            if state in states:
                counts[name] += 1
                break
        profile = profiles.get(str(conv.get("customer_id") or ""), {})
        customer = customers.get(str(conv.get("customer_id") or ""), {})
        waiting = _waiting_seconds(str(conv.get("updated_at") or ""))
        last = db.last_message(conv["id"], tenant_id=auth.tenant_id) or {}
        item = {
            "id": conv["id"],
            "state": state,
            "status": conv.get("status"),
            "mode": conv.get("mode"),
            "customer_id": conv.get("customer_id"),
            "customer_name": customer.get("name") or "",
            "tier": customer.get("tier") or "",
            "dealer_level": profile.get("dealer_level") or "",
            "priority": _queue_priority(profile),
            "risk_level": profile.get("risk_level") or "",
            "unread": db.count_unread(conv["id"], auth.actor_id,
                                      tenant_id=auth.tenant_id),
            "waiting_seconds": waiting,
            "sla_breached": state == "PENDING_HANDOFF" and waiting > SLA_SECONDS,
            "last_message": str(conv.get("last_message") or "")[:120],
            "last_role": last.get("role") or "",
            "updated_at": conv.get("updated_at"),
            "created_at": conv.get("created_at"),
            "assigned_agent_id": conv.get("assigned_agent_id"),
            "assigned_agent_name": agents.get(
                str(conv.get("assigned_agent_id") or ""), ""),
            "handoff_reason": reasons.get(conv["id"], ""),
        }
        if item["state"] not in QUEUE_TAB_STATES[tab]:
            continue
        if priority and item["priority"] != priority:
            continue
        if query:
            haystack = " ".join([
                item["id"], item["customer_id"] or "", item["customer_name"],
                item["last_message"], item["handoff_reason"],
            ]).casefold()
            if query not in haystack:
                continue
        enriched.append(item)

    if sort == "priority":
        enriched.sort(key=lambda x: (-_priority_rank(x["priority"]),
                                     x["updated_at"] or ""))
    elif sort == "recent":
        enriched.sort(key=lambda x: x["updated_at"] or "", reverse=True)
    else:
        enriched.sort(key=lambda x: x["updated_at"] or "")
    page = enriched[cursor:cursor + limit]
    next_cursor = cursor + len(page) if cursor + len(page) < len(enriched) else 0
    return JSONResponse({
        "tab": tab,
        "counts": counts,
        "items": page,
        "next_cursor": next_cursor,
        "total": len(enriched),
        "sla_seconds": SLA_SECONDS,
    })


def _summary_suggestions(intent: str, state: str) -> list[str]:
    mapping = {
        "refund_settle": ["核对订单金额与审批阈值", "超阈值先走财务审批", "告知退款到账时效"],
        "cancel_refund": ["确认订单是否已发货", "核对退款金额与审批要求", "告知取消/退款进度"],
        "refund_status": ["查询退款单状态", "告知预计到账时间"],
        "invoice_issue": ["核对开票抬头与金额", "说明开票时效"],
        "invoice_status": ["查询开票记录", "告知当前进度"],
        "aftersale_eligibility": ["核对保修期与责任判定", "引用政策条款告知结论"],
        "aftersale_apply": ["确认订单/SKU/故障", "提交售后并同步处理时效"],
        "ticket_create": ["确认故障与联系人", "创建工单并分派仓库/财务"],
        "ticket_query": ["查询工单状态", "同步处理节点"],
        "fulfill_track": ["查询最新物流节点", "告知预计送达时间"],
        "order_amend": ["确认订单状态是否可改", "发起两阶段确认"],
        "credit_query": ["核对授信与账期", "提示到期日"],
        "reconciliation_query": ["核对账期与金额", "提供对账单/开票状态"],
        "request_human": ["查看交接包原因", "向客户确认诉求并给出处理方案"],
    }
    if intent in mapping:
        return mapping[intent]
    if state == "PENDING_HANDOFF":
        return ["查看交接包原因", "回复客户并确认诉求"]
    return ["先确认客户诉求", "用客户 360 核对历史订单/工单", "无法解决时转主管或财务"]


def _build_summary_payload(auth: AuthContext, conv: dict) -> tuple[dict, int, str]:
    cid = conv["id"]
    tenant = auth.tenant_id
    customers, profiles = _business_customer_maps(tenant)
    customer_id = str(conv.get("customer_id") or "")
    customer = customers.get(customer_id, {})
    profile = profiles.get(customer_id, {})
    runs = db.list_agent_runs(cid, tenant_id=tenant, limit=1)
    run = runs[0] if runs else {}
    msgs = db.list_messages(cid, tenant_id=tenant)
    last_customer = next((m for m in reversed(msgs)
                          if m.get("role") == "customer"), {})
    handoff = db.get_handoff(cid, tenant_id=tenant) or {}
    package = handoff.get("package") or {}
    pending = db.list_pending_actions(cid, tenant_id=tenant)
    intent = str(run.get("intent") or "")
    state = str(conv.get("state") or "")
    suggestions = _summary_suggestions(intent, state)
    request_text = str(last_customer.get("content") or "")[:300]
    summary_text = (
        f"客户 {customer.get('name') or customer_id or '未登记'}，"
        f"等级 {customer.get('tier') or profile.get('dealer_level') or '-'}，"
        f"优先级 {_queue_priority(profile)}；当前状态 {state}。"
        f"最近诉求：{request_text or '暂无客户消息'}。"
        f"建议：{suggestions[0]}。"
    )
    payload = {
        "summary_text": summary_text,
        "conversation": {
            "id": cid, "state": state, "status": conv.get("status"),
            "mode": conv.get("mode"),
            "assigned_agent_id": conv.get("assigned_agent_id"),
        },
        "customer": {
            "customer_id": customer_id,
            "name": customer.get("name") or "",
            "tier": customer.get("tier") or "",
            "dealer_level": profile.get("dealer_level") or "",
            "discount": profile.get("discount"),
            "credit_limit": profile.get("credit_limit"),
            "credit_used": profile.get("credit_used"),
            "credit_remaining": round(
                float(profile.get("credit_limit") or 0)
                - float(profile.get("credit_used") or 0), 2),
            "credit_days": profile.get("credit_days"),
            "credit_due_date": profile.get("credit_due_date"),
            "priority": _queue_priority(profile),
            "risk_level": profile.get("risk_level") or "",
            "account_manager": profile.get("account_manager") or "",
        },
        "request": request_text,
        "intent": intent,
        "suggested_actions": suggestions,
        "handoff": {
            "status": handoff.get("status"),
            "reasons": package.get("reasons") or [],
            "created_at": handoff.get("created_at"),
        },
        "pending_actions": pending,
        "stats": {
            "unread": db.count_unread(cid, auth.actor_id, tenant_id=tenant),
            "waiting_seconds": _waiting_seconds(str(conv.get("updated_at") or "")),
            "message_count": len(msgs),
            "customer_messages": sum(1 for m in msgs
                                     if m.get("role") == "customer"),
        },
        "last_run": {
            "run_id": run.get("run_id"), "intent": intent,
            "model": run.get("model"), "finished_at": run.get("finished_at"),
        },
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    return payload, db.max_message_id(cid, tenant_id=tenant), str(run.get("run_id") or "")


async def api_conversation_summary(request):  # noqa: ANN201
    """会话摘要 + 客户关键信息 + 本次诉求 + 建议处理方式（缓存 + 审计）。"""
    auth = auth_from_request(request)
    denied = _workbench_or_403(auth)
    if denied:
        return denied
    cid = request.path_params["cid"]
    try:
        conv = _conv_for(auth, cid)
    except KeyError:
        return JSONResponse({"error": "not_found"}, status_code=404)
    except PermissionError:
        return _forbidden()
    refresh = request.query_params.get("refresh") in ("1", "true", "yes")
    last_id = db.max_message_id(cid, tenant_id=auth.tenant_id)
    runs = db.list_agent_runs(cid, tenant_id=auth.tenant_id, limit=1)
    run_id = str((runs[0] if runs else {}).get("run_id") or "")
    cache = db.get_summary_cache(cid, tenant_id=auth.tenant_id)
    cached = False
    if cache and not refresh and cache.get("source_message_id") == last_id             and (cache.get("source_run_id") or "") == run_id:
        generated = str(cache.get("generated_at") or "")
        try:
            age = (datetime.now(timezone.utc)
                   - datetime.fromisoformat(generated)).total_seconds()
        except ValueError:
            age = 9999
        if age <= 300:
            cached = True
    if cached:
        payload = cache.get("payload") or {}
        payload.setdefault("generated_at", cache.get("generated_at"))
    else:
        payload, last_id, run_id = _build_summary_payload(auth, conv)
        db.upsert_summary_cache(
            cid, payload, tenant_id=auth.tenant_id,
            source_message_id=last_id, source_run_id=run_id,
        )
    audit_from_auth(
        auth, "console.summary", target=cid,
        params={"cached": cached, "source_message_id": last_id},
        context_id=cid,
    )
    return JSONResponse({"summary": payload, "cached": cached})


async def api_conversation_notes(request):  # noqa: ANN201
    """内部备注：客户不可见；GET 列表 / POST 新增，均审计。"""
    auth = auth_from_request(request)
    denied = _workbench_or_403(auth)
    if denied:
        return denied
    cid = request.path_params["cid"]
    try:
        _conv_for(auth, cid)
    except KeyError:
        return JSONResponse({"error": "not_found"}, status_code=404)
    except PermissionError:
        return _forbidden()
    if request.method == "GET":
        notes = db.list_notes(cid, tenant_id=auth.tenant_id)
        audit_from_auth(auth, "console.notes_view", target=cid,
                        params={"count": len(notes)}, context_id=cid)
        return JSONResponse({"notes": notes})
    body = await request.json()
    content = str(body.get("content") or "").strip()[:2000]
    if not content:
        return JSONResponse({"error": "empty"}, status_code=400)
    note = db.add_note(cid, content, tenant_id=auth.tenant_id,
                       author_id=auth.actor_id)
    audit_from_auth(auth, "console.note_create", target=cid,
                    params={"note_id": note.get("id")}, context_id=cid)
    return JSONResponse(note, status_code=201)


async def api_reject_answer(request):  # noqa: ANN201
    """驳回 Agent 答复：存 feedback（原答复/编辑后/原因）并以坐席身份发送。"""
    auth = auth_from_request(request)
    denied = _workbench_or_403(auth)
    if denied:
        return denied
    cid = request.path_params["cid"]
    try:
        conv = _conv_for(auth, cid)
    except KeyError:
        return JSONResponse({"error": "not_found"}, status_code=404)
    except PermissionError:
        return _forbidden()
    if conv.get("mode") != "human":
        return JSONResponse({"error": "not_in_human_mode"}, status_code=409)
    if auth.role == "agent" and conv.get("assigned_agent_id") not in (
            None, auth.actor_id):
        return _forbidden("assigned to another agent")
    body = await request.json()
    try:
        message_id = int(body.get("message_id") or 0)
    except (TypeError, ValueError):
        return JSONResponse({"error": "bad_message_id"}, status_code=400)
    edited = str(body.get("edited_answer") or "").strip()[:2000]
    reason = str(body.get("reason") or "").strip()[:500]
    if not message_id or not edited or not reason:
        return JSONResponse({"error": "missing_params"}, status_code=400)
    target = db.get_message(message_id, tenant_id=auth.tenant_id)
    if target is None or target.get("conversation_id") != cid:
        return JSONResponse({"error": "not_found", "detail": "message"},
                            status_code=404)
    if target.get("role") != "ai":
        return JSONResponse({"error": "not_agent_answer"}, status_code=400)
    feedback = db.create_feedback(
        cid, message_id, reason, tenant_id=auth.tenant_id,
        created_by=auth.actor_id,
        agent_answer=str(target.get("content") or "")[:500],
        edited_answer=edited[:500],
    )
    msg = db.add_message(cid, "agent", edited, tenant_id=auth.tenant_id)
    audit_from_auth(
        auth, "console.reject_answer", target=f"message:{message_id}",
        params={"reason": reason, "feedback_id": feedback.get("id")},
        context_id=cid,
    )
    await manager.broadcast(cid, _evt(cid, msg, tenant=auth.tenant_id))
    return JSONResponse({"feedback": feedback, "message": msg})


async def api_agents(request):  # noqa: ANN201
    """可转交的坐席列表（租户内），带当前未关闭会话负载。"""
    auth = auth_from_request(request)
    denied = _workbench_or_403(auth)
    if denied:
        return denied
    loads = db.assignment_counts(tenant_id=auth.tenant_id)
    users = db.list_users(
        tenant_id=auth.tenant_id, roles=("agent", "supervisor", "admin"))
    return JSONResponse({"agents": [{
        "user_id": u["user_id"], "username": u["username"],
        "display_name": u.get("display_name") or u["user_id"],
        "role": u["role"], "status": u["status"],
        "load": loads.get(u["user_id"], 0),
    } for u in users]})


async def api_transfer(request):  # noqa: ANN201
    """把会话转交给另一名坐席/主管。"""
    auth = auth_from_request(request)
    denied = _workbench_or_403(auth)
    if denied:
        return denied
    cid = request.path_params["cid"]
    try:
        conv = _conv_for(auth, cid)
    except KeyError:
        return JSONResponse({"error": "not_found"}, status_code=404)
    except PermissionError:
        return _forbidden()
    if conv.get("state") == "CLOSED" or conv.get("status") == "closed":
        return JSONResponse({"error": "conversation_closed"}, status_code=409)
    if auth.role == "agent" and conv.get("assigned_agent_id") not in (
            None, auth.actor_id):
        return _forbidden("assigned to another agent")
    body = await request.json()
    to_agent_id = str(body.get("to_agent_id") or "").strip()
    target = db.get_user(auth.tenant_id, to_agent_id)
    if target is None or target.get("role") not in WORKBENCH_ROLES:
        return JSONResponse({"error": "bad_agent"}, status_code=400)
    if target.get("status") != "active":
        return JSONResponse({"error": "agent_inactive"}, status_code=409)
    db.update_conversation(
        cid, tenant_id=auth.tenant_id, mode="human",
        assigned_agent_id=to_agent_id,
    )
    if conv.get("status") == "pending_handoff":
        db.take_handoff(cid, tenant_id=auth.tenant_id, taken_by=to_agent_id)
    await _set_conv_state(cid, "HUMAN_ACTIVE", auth,
                          note=f"transfer:{to_agent_id}")
    name = target.get("display_name") or to_agent_id
    msg = db.add_message(
        cid, "system", f"会话已转交给 {name}，请稍候。", tenant_id=auth.tenant_id,
    )
    audit_from_auth(
        auth, "console.transfer", target=cid,
        params={"to_agent_id": to_agent_id, "conversation_id": cid},
        context_id=cid,
    )
    await manager.broadcast(cid, _evt(cid, msg, tenant=auth.tenant_id))
    return JSONResponse(db.get_conversation(cid, tenant_id=auth.tenant_id))


async def api_quick_replies(request):  # noqa: ANN201
    auth = auth_from_request(request)
    denied = _workbench_or_403(auth)
    if denied:
        return denied
    return JSONResponse({"quick_replies": db.list_quick_replies(
        tenant_id=auth.tenant_id)})


async def api_mark_read(request):  # noqa: ANN201
    """坐席打开会话后清除未读；不写审计（高频、无业务副作用）。"""
    auth = auth_from_request(request)
    denied = _workbench_or_403(auth)
    if denied:
        return denied
    cid = request.path_params["cid"]
    try:
        _conv_for(auth, cid)
    except KeyError:
        return JSONResponse({"error": "not_found"}, status_code=404)
    except PermissionError:
        return _forbidden()
    body = {}
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        pass
    try:
        last_id = int(body.get("last_message_id") or 0)
    except (TypeError, ValueError):
        last_id = 0
    if not last_id:
        last_id = db.max_message_id(cid, tenant_id=auth.tenant_id)
    value = db.mark_conversation_read(
        cid, auth.actor_id, last_id, tenant_id=auth.tenant_id,
    )
    return JSONResponse({"last_read_message_id": value})


# ---------------------------------------------------------------------------

def create_app() -> Starlette:  # noqa: ANN201
    s = get_settings()
    card = build_agent_card(
        name="人工坐席 Agent",
        description="接收编排侧交接包（handoff.accept），提供坐席工作台与模拟客户页",
        url=s.my_url(s.console_port),
        skills=[{
            "id": "handoff.accept",
            "name": "接管转人工",
            "description": "接收结构化交接包并进入坐席待接管队列（PRD 7.12）",
            "tags": ["humandesk"],
        }],
        streaming=True,
    )
    app = build_a2a_app(card, HumanDeskExecutor())
    app.router.routes.extend([
        Route("/", page_workbench),
        Route("/customer", page_customer),
        Route("/portal", page_portal),
        Mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static"),
        WebSocketRoute("/ws/{cid}", ws_chat),
        Route("/api/auth/config", api_auth_config),
        Route("/api/auth/login", api_login, methods=["POST"]),
        Route("/api/auth/logout", api_logout, methods=["POST"]),
        Route("/api/auth/me", api_me),
        Route("/api/conversations", api_conversations),
        Route("/api/conversations/{cid}/messages", api_messages),
        Route("/api/conversations/{cid}/handoff", api_handoff),
        Route("/api/conversations/{cid}/pending-actions", api_pending_actions),
        Route("/api/conversations/{cid}/customer360", api_customer360),
        Route("/api/conversations/{cid}/trace", api_trace),
        Route("/api/traces/{trace_id}", api_trace_spans),
        Route("/api/conversations/{cid}/close", api_close, methods=["POST"]),
        Route("/api/conversations/{cid}/review", api_review, methods=["POST"]),
        Route("/api/conversations/{cid}/feedback", api_feedback, methods=["POST"]),
        Route("/api/attachments", api_attachment_upload, methods=["POST"]),
        Route("/api/attachments/{aid}", api_attachment_download),
        Route("/api/tickets", api_ticket_create, methods=["POST"]),
        Route("/api/tickets", api_ticket_list),
        Route("/api/tickets/{ticket_no}", api_ticket_patch, methods=["PATCH"]),
        Route("/api/tickets/{ticket_no}/assign", api_ticket_assign, methods=["POST"]),
        Route("/api/stats/summary", api_stats_summary),
        Route("/api/conversations/{cid}/card-action", api_card_action, methods=["POST"]),
        Route("/api/knowledge", api_knowledge_search),
        Route("/api/knowledge", api_knowledge_upsert, methods=["POST"]),
        Route("/api/knowledge/{entry_id}/versions", api_knowledge_versions),
        Route("/api/policy/drafts", api_policy_drafts),
        Route("/api/policy/drafts", api_policy_draft_create, methods=["POST"]),
        Route("/api/policy/drafts/{draft_id}", api_policy_draft_detail),
        Route("/api/policy/drafts/{draft_id}/{action}",
              api_policy_draft_action, methods=["POST"]),
        Route("/api/policy/documents", api_policy_document_import,
              methods=["POST"]),
        Route("/api/conversations/{cid}/actions", api_resume_action, methods=["POST"]),
        Route("/api/conversations/{cid}/takeover", api_takeover, methods=["POST"]),
        Route("/api/conversations/{cid}/reply", api_agent_reply, methods=["POST"]),
        Route("/api/conversations/{cid}/return", api_return, methods=["POST"]),
        Route("/api/customer/send", api_customer_send, methods=["POST"]),
        Route("/api/customer/conversation", api_customer_conversation),
        Route("/api/customer/history", api_conversation_history),
        Route("/api/customer/conversations/{cid}/handoff", api_customer_handoff,
              methods=["POST"]),
        Route("/api/tools", api_tools),
        Route("/api/tools/{tid}/invoke", api_tool_invoke, methods=["POST"]),
        Route("/api/audit", api_audit),
        Route("/api/audit/verify", api_audit_verify),
        Route("/api/audit/events", api_audit_events),
        Route("/api/audit/events/verify", api_audit_events_verify),
        Route("/api/approvals", api_approvals),
        Route("/api/approvals/{aid}/approve", api_approve, methods=["POST"]),
        Route("/api/approvals/{aid}/reject", api_reject, methods=["POST"]),
        Route("/api/approvals/{aid}/execute", api_approval_execute, methods=["POST"]),
        Route("/api/assist/ask", api_assist_ask, methods=["POST"]),
        Route("/api/assist/history", api_assist_history),
        # Phase 4：内部坐席工作台
        Route("/api/workbench/queues", api_workbench_queues),
        Route("/api/conversations/{cid}/summary", api_conversation_summary),
        Route("/api/conversations/{cid}/notes", api_conversation_notes,
              methods=["GET", "POST"]),
        Route("/api/conversations/{cid}/reject-answer", api_reject_answer,
              methods=["POST"]),
        Route("/api/conversations/{cid}/read", api_mark_read, methods=["POST"]),
        Route("/api/agents", api_agents),
        Route("/api/conversations/{cid}/transfer", api_transfer,
              methods=["POST"]),
        Route("/api/quick-replies", api_quick_replies),
    ])
    app.add_middleware(
        build_auth_middleware(open_paths=("/", "/customer", "/static"))
    )
    app.add_middleware(SecurityHeadersMiddleware)
    return app


app = create_app()
