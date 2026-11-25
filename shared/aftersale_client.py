"""售后域内部 client（console 坐席工单 CRUD 的后端通道）。

console 不再直连业务库 SQL——工单逻辑单份实现在 aftersale 域
/internal API，本 client 只是带 JWT 的同步 HTTP 薄封装（同政策域
PolicyClient 先例；坐席 UI 的同步操作不建 A2A Task）。

返回 (status_code, payload)；HTTP 传输层错误抛 AftersaleUnavailable，
调用方（console handler）转 503。
"""

from __future__ import annotations

import httpx

from shared.auth import client_headers
from shared.config import get_settings


class AftersaleUnavailable(Exception):
    """售后 Server 不可达/超时——console 转为 503，不得本地伪造结果。"""


def _call(method: str, path: str, *, auth, timeout: float = 5.0,
          params: dict | None = None, json_body: dict | None = None,
          ) -> tuple[int, dict]:  # noqa: ANN001
    s = get_settings()
    base = s.domain_url("aftersale").rstrip("/")
    try:
        resp = httpx.request(
            method, f"{base}{path}", params=params, json=json_body,
            headers=client_headers(auth), timeout=timeout, trust_env=False,
        )
    except httpx.HTTPError as e:
        raise AftersaleUnavailable(f"售后内部 API 调用失败：{e}") from e
    try:
        payload = resp.json()
    except ValueError:
        payload = {"error": "bad_response"}
    return resp.status_code, payload


def create_ticket(auth, body: dict, timeout: float = 5.0) -> tuple[int, dict]:  # noqa: ANN001
    return _call("POST", "/internal/tickets", auth=auth, timeout=timeout,
                 json_body=body)


def list_tickets(auth, params: dict | None = None,
                 timeout: float = 5.0) -> tuple[int, dict]:  # noqa: ANN001
    return _call("GET", "/internal/tickets", auth=auth, timeout=timeout,
                 params=params or {})


def patch_ticket(auth, ticket_no: str, body: dict,
                 timeout: float = 5.0) -> tuple[int, dict]:  # noqa: ANN001
    return _call("PATCH", f"/internal/tickets/{ticket_no}", auth=auth,
                 timeout=timeout, json_body=body)


def assign_ticket(auth, ticket_no: str, body: dict,
                  timeout: float = 5.0) -> tuple[int, dict]:  # noqa: ANN001
    return _call("POST", f"/internal/tickets/{ticket_no}/assign", auth=auth,
                 timeout=timeout, json_body=body)
