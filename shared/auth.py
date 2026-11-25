"""JWT 身份、租户上下文与工具级权限（PRD 8.1/8.2 的工程落地）。

关键变化（相对 M1 的 INTERNAL_TOKEN 单证）：
- 用户/坐席/服务身份统一为 JWT：tenant_id / actor_id / role / scopes 全部来自签名 claim；
- HTTP 中间件验签后把 AuthContext 放进 request.state 与 ContextVar；
- 工具只认 token claim，不再接受 metadata 里自称的 scope；
- 默认拒绝：没有有效 AuthContext 一律拒绝（不再有“未带 scope = 内部信任”）。

JWT 用 HS256 + shared/keys.py 的密钥抽象；生产由 KMS 提供密钥，禁止默认密钥。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from contextvars import ContextVar, Token
from http.cookies import SimpleCookie
from dataclasses import dataclass, field
from typing import Any, Iterable

from starlette.requests import Request
from starlette.responses import JSONResponse

from a2a.server.routes.common import DefaultServerCallContextBuilder

from shared.config import Settings, get_settings
from shared.keys import get_signing_key

OPEN_PATHS = (
    "/.well-known",
    "/health",
    "/api/auth/login",
    "/api/auth/config",
    "/static",
    "/customer",
    "/portal",
    "/login",
)


class AuthError(Exception):
    """身份缺失/签名错误/过期/越权。"""


@dataclass(frozen=True)
class AuthContext:
    tenant_id: str
    actor_id: str
    role: str
    scopes: frozenset[str] = field(default_factory=frozenset)
    token: str = ""
    on_behalf_of: str = ""
    token_type: str = "user"

    def has_scope(self, scope: str) -> bool:
        if "*" in self.scopes or scope in self.scopes:
            return True
        if scope.endswith(".read"):
            return scope[: -len(".read")] + ".write" in self.scopes
        return False

    def as_metadata(self) -> dict[str, Any]:
        """进 graph/tool config 的安全投影：绝不包含原始 token。"""
        return {
            "tenant_id": self.tenant_id,
            "actor_id": self.actor_id,
            "role": self.role,
            "scopes": sorted(self.scopes),
            "on_behalf_of": self.on_behalf_of,
            "token_type": self.token_type,
        }


_current_auth: ContextVar[AuthContext | None] = ContextVar(
    "agentdock_current_auth", default=None
)


def set_current_auth(auth: AuthContext | None) -> Token:
    return _current_auth.set(auth)


def reset_current_auth(token: Token) -> None:
    _current_auth.reset(token)


def current_auth() -> AuthContext | None:
    return _current_auth.get()


# ---------------------------------------------------------------------------
# JWT（HS256，stdlib 实现，避免新增运行时依赖）
# ---------------------------------------------------------------------------

def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(value: str) -> bytes:
    pad = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + pad)


def _sign(signing_input: bytes) -> str:
    key = get_signing_key()
    return _b64e(hmac.new(key.material, signing_input, hashlib.sha256).digest())


def create_token(
    *,
    tenant_id: str,
    actor_id: str,
    role: str,
    scopes: Iterable[str],
    ttl_seconds: int | None = None,
    on_behalf_of: str = "",
    token_type: str = "user",
    issuer: str | None = None,
    audience: str = "",
    extra_claims: dict | None = None,
) -> str:
    s = get_settings()
    now = int(time.time())
    ttl = ttl_seconds if ttl_seconds is not None else s.auth_token_ttl_minutes * 60
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "iss": issuer or s.auth_issuer,
        "sub": actor_id,
        "tenant_id": tenant_id,
        "role": role,
        "scopes": sorted(set(scopes)),
        "typ": token_type,
        "iat": now,
        "exp": now + ttl,
        "jti": secrets.token_urlsafe(12),
    }
    if on_behalf_of:
        payload["on_behalf_of"] = on_behalf_of
    if audience:
        payload["aud"] = audience
    if extra_claims:
        payload.update(extra_claims)
        # 标准 claim 不可被 extra 覆盖（action token 场景防手滑）
        for key, value in (
            ("iss", payload["iss"]), ("sub", payload["sub"]),
            ("tenant_id", payload["tenant_id"]), ("role", payload["role"]),
            ("scopes", payload["scopes"]), ("typ", payload["typ"]),
            ("exp", payload["exp"]),
        ):
            payload[key] = value
    signing_input = (
        _b64e(json.dumps(header, separators=(",", ":")).encode("utf-8"))
        + "."
        + _b64e(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    ).encode("ascii")
    return signing_input.decode("ascii") + "." + _sign(signing_input)


def decode_token(token: str, *, expected_audience: str = "") -> AuthContext:
    if not token:
        raise AuthError("missing token")
    try:
        head_b64, body_b64, sig = token.split(".")
    except ValueError as exc:
        raise AuthError("malformed token") from exc
    signing_input = f"{head_b64}.{body_b64}".encode("ascii")
    expected = _sign(signing_input)
    if not hmac.compare_digest(expected, sig):
        raise AuthError("bad signature")
    try:
        header = json.loads(_b64d(head_b64))
        payload = json.loads(_b64d(body_b64))
    except (ValueError, json.JSONDecodeError) as exc:
        raise AuthError("malformed payload") from exc
    if header.get("alg") != "HS256":
        raise AuthError("unsupported alg")
    if payload.get("iss") != get_settings().auth_issuer:
        raise AuthError("bad issuer")
    if expected_audience and str(payload.get("aud", "")) != expected_audience:
        raise AuthError("bad audience")
    if int(payload.get("exp", 0)) < int(time.time()):
        raise AuthError("token expired")
    scopes = payload.get("scopes") or []
    if not isinstance(scopes, list):
        raise AuthError("bad scopes")
    return AuthContext(
        tenant_id=str(payload.get("tenant_id", "")),
        actor_id=str(payload.get("sub", "")),
        role=str(payload.get("role", "")),
        scopes=frozenset(str(s) for s in scopes),
        token=token,
        on_behalf_of=str(payload.get("on_behalf_of", "")),
        token_type=str(payload.get("typ", "user")),
    )


def decode_action_token(token: str, *, expected_audience: str = "orchestrator") -> dict | None:
    """验签 action token（typ=action）；任何失败返回 None，调用方必须忽略。"""
    if not token:
        return None
    try:
        ctx = decode_token(token, expected_audience=expected_audience)
    except AuthError:
        return None
    try:
        head_b64, body_b64, _ = token.split(".")
        payload = json.loads(_b64d(body_b64))
    except (ValueError, json.JSONDecodeError):
        return None
    if payload.get("typ") != "action":
        return None
    if ctx.tenant_id != str(payload.get("tenant_id", "")) or ctx.actor_id != str(
        payload.get("sub", "")
    ):
        return None
    return payload


def auth_from_headers(
    headers: dict[str, str] | Any, *, expected_audience: str = ""
) -> AuthContext | None:
    """从 Starlette/ASGI headers 取 Bearer 并验签；无 token 返回 None。"""
    if not headers:
        return None
    if hasattr(headers, "get"):
        raw = headers.get("authorization") or headers.get("Authorization") or ""
    else:
        raw = ""
    if not raw.lower().startswith("bearer "):
        return None
    return decode_token(
        raw.split(" ", 1)[1].strip(), expected_audience=expected_audience,
    )


COOKIE_SURFACES = ("customer", "workbench")


def cookie_prefix() -> str:
    return "__Secure-" if get_settings().env == "prod" else ""


def session_cookie_name(surface: str) -> str:
    return f"{cookie_prefix()}agentdock_{surface}_session"


def csrf_cookie_name(surface: str) -> str:
    return f"{cookie_prefix()}agentdock_{surface}_csrf"


def auth_from_cookie_values(
    cookies: dict[str, str], *, surface: str = "",
) -> tuple[AuthContext | None, str, str]:
    """按 surface 优先解析会话 cookie；返回 (auth, surface, prefix)。"""
    order = ([surface] if surface else []) + [
        s for s in COOKIE_SURFACES if s != surface
    ]
    for name in order:
        raw = cookies.get(session_cookie_name(name))
        if not raw:
            continue
        try:
            return decode_token(raw), name, cookie_prefix()
        except AuthError:
            continue
    return None, "", cookie_prefix()


def csrf_ok(
    headers: dict[str, str], cookies: dict[str, str], *, surface: str, prefix: str,
) -> bool:
    header = str(headers.get("x-csrf-token") or "")
    cookie = str(cookies.get(f"{prefix}agentdock_{surface}_csrf") or "")
    return bool(header and cookie and hmac.compare_digest(header, cookie))


def auth_from_request(request: Request) -> AuthContext | None:
    auth = getattr(request.state, "auth", None)
    if isinstance(auth, AuthContext):
        return auth
    return None


class AuthServerCallContextBuilder(DefaultServerCallContextBuilder):
    """把 JWT 解出的 tenant/auth 放进 A2A ServerCallContext。

    - `tenant` 供 TaskStore 做租户隔离；
    - `state["auth"]` 供 executor 直接使用，避免重复解 header；
    - 保留 headers，兼容现有 executor 的兜底路径。
    """

    def build(self, request: Request):  # noqa: ANN201
        ctx = super().build(request)
        try:
            auth = auth_from_headers(dict(request.headers))
        except AuthError:
            auth = None
        if auth is not None:
            ctx.tenant = auth.tenant_id
            ctx.state["auth"] = auth
        return ctx


def auth_from_config(config: dict | None) -> AuthContext | None:
    """工具/图节点读取身份。

    只接受两种来源：
    1. `metadata.auth_token`：必须是验签通过的 JWT（A2A/脚本/测试都走这条）；
    2. 当前请求的 ContextVar（executor/HTTP 中间件在验签后设置）。

    不再接受 `metadata.auth` 这种“自称 claim”的字典，避免进程内调用伪造身份。
    """
    meta = (config or {}).get("metadata") or {}
    token = meta.get("auth_token")
    if token:
        return decode_token(str(token))
    return current_auth()


def _require_test_env() -> None:
    if get_settings().env != "test":
        raise AuthError("auth_config is test-only; use a signed token")


def auth_metadata(
    *scopes: str,
    tenant_id: str = "t_demo",
    actor_id: str = "test-user",
    role: str = "service",
    on_behalf_of: str = "",
) -> dict:
    """测试 helper：构造含真实签名 JWT 的 metadata（仅 ENV=test 可用）。"""
    _require_test_env()
    token = create_token(
        tenant_id=tenant_id,
        actor_id=actor_id,
        role=role,
        scopes=list(scopes or ALL_SCOPES),
        ttl_seconds=3600,
        on_behalf_of=on_behalf_of,
        token_type="service" if role == "service" else "user",
    )
    return {"auth_token": token, "customer_id": on_behalf_of}


def auth_config(*scopes: str, **kwargs) -> dict:
    """测试 helper：返回可直接传给 LangChain tool.invoke 的 config（签名 token）。"""
    return {"metadata": auth_metadata(*scopes, **kwargs)}


def require_roles(config: dict | None, roles: Iterable[str]) -> AuthContext:
    auth = auth_from_config(config)
    if auth is None:
        raise AuthError("missing auth context")
    if auth.role not in set(roles):
        raise AuthError(f"role {auth.role} not allowed")
    return auth


# ---------------------------------------------------------------------------
# 工具级权限（默认拒绝，write 覆盖 read）
# ---------------------------------------------------------------------------

WRITE_SCOPES = {
    "ticket.create", "order.amend", "order.cancel", "order.restore",
    "fulfill.atp", "invoice.issue", "refund.settle",
}

# 工具名 → 风险级别（high=写/不可逆，mid=对外承诺，low=只读）
TOOL_RISK = {
    "create_ticket": "high",
    "order_amend": "high",
    "cancel_order": "high",
    "restore_order": "high",
    "refund_settle": "high",
    "invoice_issue": "high",
    "judge_eligibility": "mid",
    "determine_responsibility": "mid",
    "answer_policy": "mid",
    "get_clause": "low",
    "product_lookup": "low",
    "product_retrieve": "low",
    "compare_matrix": "low",
    "promo_evaluate": "low",
    "query_ticket": "low",
    "billing_statement": "mid",
    "refund_status_query": "low",
    "order_query": "low",
    "fulfill_track": "low",
    "fulfill_atp": "low",
}


def required_scope(domain: str, skill: str) -> str:
    return f"{domain}.{'write' if skill in WRITE_SCOPES else 'read'}"


def ensure_scope(config: dict | None, scope: str) -> bool:
    """工具级权限校验：只认 token claim；无身份/无 scope 一律拒绝。"""
    auth = auth_from_config(config)
    if auth is None:
        return False
    return auth.has_scope(scope)


def ensure_roles(config: dict | None, roles: Iterable[str]) -> bool:
    auth = auth_from_config(config)
    return auth is not None and auth.role in set(roles)


# ---------------------------------------------------------------------------
# 服务间调用头：转发调用方 token（on-behalf-of），不再使用 INTERNAL_TOKEN
# ---------------------------------------------------------------------------

def client_headers(auth: AuthContext | None = None) -> dict[str, str]:
    auth = auth or current_auth()
    if auth is None:
        # 仅测试进程兜底：生成带全部 scope 的短时服务 token，仍然经过验签与 scope 校验。
        # dev/prod 未显式传身份一律失败，避免“忘了传 token 就自动全权限”。
        s = get_settings()
        if s.env != "test":
            raise AuthError("no auth context for outbound call")
        auth = AuthContext(
            tenant_id="t_demo",
            actor_id="test-service",
            role="service",
            scopes=frozenset(ALL_SCOPES),
            token=create_token(
                tenant_id="t_demo",
                actor_id="test-service",
                role="service",
                scopes=ALL_SCOPES,
                token_type="service",
            ),
            token_type="service",
        )
    return {"Authorization": f"Bearer {auth.token}"}


# ---------------------------------------------------------------------------
# 密码哈希（控制台登录）
# ---------------------------------------------------------------------------

_PBKDF2_ITERATIONS = 260_000


def hash_password(password: str, *, iterations: int = _PBKDF2_ITERATIONS) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return (
        f"pbkdf2_sha256${iterations}${_b64e(salt)}${_b64e(digest)}"
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iterations, salt_b64, digest_b64 = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        salt = _b64d(salt_b64)
        expected = _b64d(digest_b64)
        got = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, int(iterations)
        )
        return hmac.compare_digest(got, expected)
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# 默认角色 scope（seed 与测试共用）
# ---------------------------------------------------------------------------

DOMAIN_SCOPES = (
    "merchandising.read", "order.read", "fulfillment.read",
    "aftersale.read", "finance.read", "policy.read", "customer.read",
)
DOMAIN_WRITE_SCOPES = (
    "merchandising.write", "order.write", "fulfillment.write",
    "aftersale.write", "finance.write", "policy.write",
)
# 营销经营分析（toB 商家服务端）：不进 DOMAIN_SCOPES——ROLE_SCOPES["customer"]
# 以 DOMAIN_SCOPES 为基，进去了就等于给 C 端客户放开经营分析（越权）。
MARKETING_SCOPES = ("marketing.read", "marketing.write")
ALL_SCOPES = DOMAIN_SCOPES + DOMAIN_WRITE_SCOPES + MARKETING_SCOPES + (
    "handoff.accept", "audit.read", "approval.read", "approval.write",
    "knowledge.write",
)

ROLE_SCOPES: dict[str, tuple[str, ...]] = {
    # 客户：可以发起业务动作，但高风险动作仍需内部审批；不含 marketing（toB-only）
    "customer": DOMAIN_SCOPES + ("order.write", "aftersale.write", "finance.write",
                                 "handoff.accept"),
    # 坐席：可查可改地址/建单；退款/开票需主管/财务审批；含营销分析（商家服务端）
    "agent": DOMAIN_SCOPES + MARKETING_SCOPES
             + ("order.write", "aftersale.write", "finance.write",
                "handoff.accept", "approval.read", "knowledge.write"),
    "supervisor": ALL_SCOPES,
    "finance": ("order.read", "finance.read", "finance.write", "policy.read",
                "approval.read", "approval.write"),
    "admin": ALL_SCOPES,
    "service": ALL_SCOPES,
}


# ---------------------------------------------------------------------------
# ASGI JWT 中间件（HTTP；WebSocket 由端点做首包认证）
# ---------------------------------------------------------------------------

def _is_open_path(path: str, prefixes: tuple[str, ...]) -> bool:
    for prefix in prefixes:
        if prefix == "/":
            if path == "/":
                return True
        elif path.startswith(prefix):
            return True
    return False


class JWTAuthMiddleware:
    """纯 ASGI 中间件：验签、注入 request.state.auth、设置 ContextVar。

    WebSocket 不在这里拒绝：浏览器 WS 不能带 Authorization 头，必须由端点
    完成“连接后首包 auth”认证；中间件只负责 HTTP。
    """

    def __init__(self, app: Any, open_paths: tuple[str, ...] = ()) -> None:  # noqa: ANN401
        self.app = app
        self.open_paths = tuple(open_paths)

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:  # noqa: ANN401
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if _is_open_path(path, OPEN_PATHS + self.open_paths):
            await self.app(scope, receive, send)
            return
        headers = {
            k.decode("latin-1").lower(): v.decode("latin-1")
            for k, v in scope.get("headers", [])
        }
        cookies: dict[str, str] = {}
        raw_cookie = headers.get("cookie", "")
        if raw_cookie:
            try:
                jar = SimpleCookie()
                jar.load(raw_cookie)
                cookies = {k: m.value for k, m in jar.items()}
            except Exception:  # noqa: BLE001
                cookies = {}
        surface_hint = headers.get("x-surface", "")
        if surface_hint not in COOKIE_SURFACES:
            surface_hint = ""
        auth = None
        auth_source = ""
        auth_surface = ""
        prefix = cookie_prefix()
        try:
            auth = auth_from_headers(headers)
            if auth is not None:
                auth_source = "bearer"
                auth_surface = surface_hint
        except AuthError as exc:
            response = JSONResponse(
                {"error": {"code": -32001, "message": "unauthorized", "detail": str(exc)}},
                status_code=401,
            )
            await response(scope, receive, send)
            return
        if auth is None:
            auth, auth_surface, prefix = auth_from_cookie_values(
                cookies, surface=surface_hint,
            )
            auth_source = "cookie" if auth is not None else ""
        if auth is None:
            response = JSONResponse(
                {"error": {"code": -32001, "message": "unauthorized",
                           "detail": "missing bearer token or session cookie"}},
                status_code=401,
            )
            await response(scope, receive, send)
            return
        method = str(scope.get("method", "GET")).upper()
        if auth_source == "cookie" and method not in ("GET", "HEAD", "OPTIONS"):
            if not csrf_ok(headers, cookies, surface=auth_surface, prefix=prefix):
                response = JSONResponse(
                    {"error": "csrf_invalid", "detail": "X-CSRF-Token mismatch"},
                    status_code=403,
                )
                await response(scope, receive, send)
                return
        state = scope.setdefault("state", {})
        state["auth"] = auth
        state["auth_source"] = auth_source
        state["auth_surface"] = auth_surface
        token = set_current_auth(auth)
        try:
            await self.app(scope, receive, send)
        finally:
            reset_current_auth(token)


def build_auth_middleware(
    settings: Settings | None = None, open_paths: tuple = ()
):  # noqa: ANN201
    """返回可直接传给 Starlette.add_middleware 的中间件类。"""
    _ = settings
    allowed = tuple(open_paths)

    class _Configured(JWTAuthMiddleware):
        def __init__(self, app: Any) -> None:  # noqa: ANN401
            super().__init__(app, allowed)

    return _Configured
