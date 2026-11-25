"""编排层 → 域 Server 的 A2A 客户端封装（技术方案 2.4 委托语义的 M1 实现）。

- 新委托：SendMessage（text part 携带任务描述 + metadata 透传 trace_id）
- 续聊/回填：同 taskId 再发一条消息（域侧执行器识别 input-required → resume）
- 返回 DomainResult：终态、文本、DataPart 列表、Task 快照
"""

import logging
import threading
import time
from dataclasses import dataclass, field

import httpx

import a2a.helpers as ah
from a2a.client import ClientConfig, ClientFactory
from a2a.types.a2a_pb2 import SendMessageRequest

from shared.audit import audit_from_auth
from shared.auth import AuthContext, client_headers, current_auth
from shared.card_signing import (
    CardVerificationError,
    SignatureVerificationError,
    build_card_verifier,
)
from shared.config import get_settings

logger = logging.getLogger(__name__)


# Card 缓存（PRD 7.3.1：拉取并缓存 Card，TTL 5 分钟）：验签通过才入缓存，
# 命中走 factory.create(card) 免重复拉取；TTL 过期重新拉取+验签。
# kid 轮换期间旧 Card 签名在 TTL 内仍被信任——轮换方必须保留旧公钥于
# CARD_VERIFY_KEYS_JSON（shared/card_signing.card_key_material）。
_CARD_CACHE: dict[str, tuple[object, float]] = {}
_CARD_CACHE_LOCK = threading.Lock()


def _card_cache_get(key: str) -> object | None:
    with _CARD_CACHE_LOCK:
        hit = _CARD_CACHE.get(key)
    if hit is None:
        return None
    card, ts = hit
    if time.monotonic() - ts < get_settings().card_cache_ttl_seconds:
        return card
    with _CARD_CACHE_LOCK:
        _CARD_CACHE.pop(key, None)
    return None


def _card_cache_put(key: str, card: object) -> None:
    with _CARD_CACHE_LOCK:
        _CARD_CACHE[key] = (card, time.monotonic())


async def _acquire_client(
    factory: ClientFactory,
    httpx_client: httpx.AsyncClient,
    base_url: str,
    *,
    auth: AuthContext | None,
    trace_id: str,
    context_id: str,
    task_id: str,
):
    """拉取（缓存优先）+ 按需验签 AgentCard → Client。

    验签失败：审计 card.verify_failed 后抛 CardVerificationError——编排层
    domain_call 的兜底 except 会把它转成该域局部交付，不拖垮整条链。
    Card 拉取本身的网络错误（AgentCardResolutionError）维持原语义直接上抛。
    """
    settings = get_settings()
    cache_key = base_url.rstrip("/")
    cached = _card_cache_get(cache_key)
    if cached is not None:
        return factory.create(cached)

    from a2a.client.card_resolver import A2ACardResolver

    verifier = build_card_verifier() if settings.card_verify_enabled else None
    try:
        card = await A2ACardResolver(httpx_client, base_url + "/").get_agent_card(
            signature_verifier=verifier
        )
    except SignatureVerificationError as e:
        audit_from_auth(
            auth, "card.verify_failed", target=base_url,
            params={"error": type(e).__name__, "detail": str(e)[:200]},
            trace_id=trace_id, context_id=context_id, task_id=task_id,
        )
        raise CardVerificationError(f"AgentCard 验签失败 {base_url}: {e}") from e
    _card_cache_put(cache_key, card)
    return factory.create(card)



@dataclass
class DomainResult:
    task_id: str
    context_id: str
    state: str  # TaskState 枚举名，如 TASK_STATE_COMPLETED
    text: str = ""
    data_parts: list = field(default_factory=list)  # 各 Artifact 的 DataPart data
    status_data: dict | None = None  # input-required 时 status message 里的 payload


def state_name(task) -> str:  # noqa: ANN001
    return task.status.state


async def delegate(
    base_url: str,
    text: str,
    *,
    task_id: str | None = None,
    context_id: str | None = None,
    metadata: dict | None = None,
    timeout: float = 60.0,
    auth: AuthContext | None = None,
) -> DomainResult:
    """向域 Server 委托一次（或对已有 Task 回填）。

    鉴权：转发调用方 JWT（on-behalf-of），不再使用 INTERNAL_TOKEN 单证。
    """
    auth = auth or current_auth()
    meta = dict(metadata or {})
    trace_id = str(meta.get("trace_id", ""))
    audit_from_auth(
        auth, "a2a.send", target=base_url,
        params={"task_id": task_id, "context_id": context_id,
                "text": text, "metadata": {k: v for k, v in meta.items()
                                           if k not in ("auth_token",)}},
        trace_id=trace_id, context_id=str(context_id or meta.get("context_id", "")),
        task_id=str(task_id or ""),
    )

    # M4（PRD 7.10）：delegate 是唯一跨进程出口——有活动 span 时把 traceparent
    # 带进请求 metadata，对端 start_server_span 以它为远端父上下文拼调用树。
    from shared.tracing import current_traceparent

    tp = current_traceparent()
    if tp:
        meta["traceparent"] = tp

    # trust_env=False：服务间调用永远走内网地址，必须绕过系统代理——
    # 实测踩坑：Windows 系统代理（注册表）开启时，httpx 默认把 127.0.0.1
    # 的内部调用劫持给代理，全部 ConnectError，整个多 Agent 协作瘫痪。
    httpx_client = httpx.AsyncClient(
        timeout=timeout, headers=client_headers(auth), trust_env=False
    )
    try:
        factory = ClientFactory(ClientConfig(httpx_client=httpx_client))
        client = await _acquire_client(
            factory, httpx_client, base_url,
            auth=auth, trace_id=trace_id,
            context_id=str(context_id or meta.get("context_id", "")),
            task_id=str(task_id or ""),
        )
        message = ah.new_text_message(text, task_id=task_id, context_id=context_id)
        req = SendMessageRequest(message=message)
        if meta:
            # a2a-sdk 1.2.x：RequestContext.metadata 读的是请求级 metadata，不是 message.metadata
            req.metadata.update(meta)

        result = DomainResult(task_id=task_id or "", context_id=context_id or "", state="")
        async for resp in client.send_message(req):
            kind = resp.WhichOneof("payload")
            if kind == "task":
                # 非流式应答（Card streaming=False，如桩域）：单个 Task 快照
                t = resp.task
                result.task_id, result.context_id = t.id, t.context_id
                result.state = t.status.state
                if t.status.message and t.status.message.parts:
                    dparts = ah.get_data_parts(t.status.message.parts)
                    if dparts:
                        result.status_data = dparts[-1]
                    tparts = ah.get_text_parts(t.status.message.parts)
                    if tparts:
                        result.text = tparts[-1]
                for artifact in t.artifacts:
                    result.data_parts.extend(ah.get_data_parts(artifact.parts))
                    texts = ah.get_text_parts(artifact.parts)
                    if texts:
                        result.text = texts[-1]
            elif kind == "status_update":
                ev = resp.status_update
                result.task_id, result.context_id = ev.task_id, ev.context_id
                result.state = ev.status.state
                if ev.status.message:
                    parts = ah.get_data_parts(ev.status.message.parts)
                    if parts:
                        result.status_data = parts[-1]
            elif kind == "artifact_update":
                ev = resp.artifact_update
                result.task_id, result.context_id = ev.task_id, ev.context_id
                result.data_parts.extend(ah.get_data_parts(ev.artifact.parts))
                texts = ah.get_text_parts(ev.artifact.parts)
                if texts:
                    result.text = texts[-1]
        audit_from_auth(
            auth, "a2a.result", target=base_url,
            params={"task_id": result.task_id, "state": str(result.state)},
            result_summary=result.text[:200], trace_id=trace_id,
            context_id=str(result.context_id or context_id or ""),
            task_id=str(result.task_id or ""),
        )
        return result
    finally:
        await httpx_client.aclose()
