"""政策工具 client（各域内嵌，进程内调用政策 Server 的 /internal 只读 API）。

不建 A2A Task（PRD 7.3.7 政策例外）：条款引用是域内推理的原料，不是协作任务。
同步 httpx——域内 @tool 是同步函数，ToolNode 在 executor 里跑。
政策 Server 不可用 → PolicyUnavailable，调用域降级为"条款暂不可查"（技术方案 3.4）。
"""

import httpx

from shared.auth import client_headers
from shared.config import get_settings


class PolicyUnavailable(Exception):
    """政策 Server 不可达/超时——调用方应降级，不得编造条款。"""


def lookup_clauses(
    query: str = "", clause_id: str = "", timeout: float = 5.0, auth=None,
) -> list[dict]:
    """检索生效条款：给 query 走关键词，给 clause_id 走精确查询。

    tenant 只从 auth token claim 取；不再接受调用方自报 tenant_id。
    """
    s = get_settings()
    base = s.domain_url("policy").rstrip("/")
    params = {"clause_id": clause_id} if clause_id else {"q": query}
    try:
        resp = httpx.get(
            f"{base}/internal/clauses", params=params,
            headers=client_headers(auth), timeout=timeout,
            trust_env=False,  # 内网调用绕过系统代理（见 shared/a2a_client.py 注释）
        )
        resp.raise_for_status()
        return resp.json().get("clauses", [])
    except httpx.HTTPError as e:
        raise PolicyUnavailable(f"政策内部 API 调用失败：{e}") from e


def cite(clause: dict) -> str:
    """条款引用串（AC-31：clause_id + version + 生效期缺一不可）。"""
    return (f"{clause.get('clause_id')}@{clause.get('version')}"
            f"（生效 {clause.get('effective_range')}）")


def _request(method: str, path: str, *, auth, timeout: float = 5.0,
             params: dict | None = None, json_body: dict | None = None,
             ) -> tuple[int, dict]:  # noqa: ANN001
    s = get_settings()
    base = s.domain_url("policy").rstrip("/")
    try:
        resp = httpx.request(
            method, f"{base}{path}", params=params, json=json_body,
            headers=client_headers(auth), timeout=timeout, trust_env=False,
        )
    except httpx.HTTPError as e:
        raise PolicyUnavailable(f"政策内部 API 调用失败：{e}") from e
    try:
        payload = resp.json()
    except ValueError:
        payload = {"error": "bad_response"}
    return resp.status_code, payload


def knowledge_search(auth, q: str = "", status: str = "published",
                     timeout: float = 5.0) -> tuple[int, dict]:  # noqa: ANN001
    """坐席知识库检索/列表（console /api/knowledge 的后端）。"""
    return _request("GET", "/internal/knowledge", auth=auth, timeout=timeout,
                    params={"q": q, "status": status})


def knowledge_upsert(auth, body: dict, timeout: float = 5.0) -> tuple[int, dict]:  # noqa: ANN001
    """坐席知识条目版本化写入（console POST /api/knowledge 的后端）。"""
    return _request("POST", "/internal/knowledge", auth=auth, timeout=timeout,
                    json_body=body)


def knowledge_versions(auth, entry_id: str,
                       timeout: float = 5.0) -> tuple[int, dict]:  # noqa: ANN001
    """知识条目历史版本（console /api/knowledge/{id}/versions 的后端）。"""
    return _request("GET", f"/internal/knowledge/{entry_id}/versions", auth=auth,
                    timeout=timeout)
