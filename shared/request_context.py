"""当前 A2A 请求的 metadata ContextVar（confirm_token/approval_id/action 等）。

LangGraph 的节点 config.metadata 在不同版本/节点类型下不保证完整透传；
executor 在验签后把请求 metadata 放入 ContextVar，工具节点统一读取，
避免“token 在编排层有、到域工具节点丢了”的隐式依赖。
"""

from __future__ import annotations

from contextvars import ContextVar, Token

_request_meta: ContextVar[dict] = ContextVar("agentdock_request_meta", default={})


def set_request_meta(meta: dict | None) -> Token:
    return _request_meta.set(dict(meta or {}))


def reset_request_meta(token: Token) -> None:
    _request_meta.reset(token)


def current_request_meta() -> dict:
    return dict(_request_meta.get() or {})


_tool_events: ContextVar[list | None] = ContextVar(
    "agentdock_tool_events", default=None
)


def begin_tool_events() -> Token:
    return _tool_events.set([])


def reset_tool_events(token: Token) -> None:
    _tool_events.reset(token)


def record_tool_event(event: dict) -> None:
    bucket = _tool_events.get()
    if bucket is not None:
        bucket.append(event)


def drain_tool_events() -> list[dict]:
    return list(_tool_events.get() or [])
