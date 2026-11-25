"""安全响应头（CSP / frame-ancestors / nosniff / HSTS 等）。

两套前端同源部署：
- customer 页允许被同源商家 portal iframe 嵌入（frame-ancestors 'self'）；
- workbench 独立页禁止被嵌入（frame-ancestors 'none'）。
样式全部外置（app.css / workbench.css），CSP 收紧为 script-src/style-src 'self'。
"""

from __future__ import annotations

from starlette.datastructures import MutableHeaders

from shared.config import get_settings


def _connect_src() -> str:
    s = get_settings()
    if s.env != "prod":
        return (
            "'self' ws://127.0.0.1:* http://127.0.0.1:* "
            "ws://localhost:* http://localhost:*"
        )
    sources = ["'self'"]
    raw = s.allowed_origins.strip()
    if raw:
        for origin in (x.strip() for x in raw.split(",") if x.strip()):
            sources.append(origin)
            if origin.startswith("https://"):
                sources.append("wss://" + origin[len("https://"):])
            elif origin.startswith("http://"):
                sources.append("ws://" + origin[len("http://"):])
    return " ".join(dict.fromkeys(sources))


def _csp(is_customer: bool) -> str:
    frame = "'self'" if is_customer else "'none'"
    return (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self'; "
        "img-src 'self' data: blob:; "
        "font-src 'self'; "
        f"connect-src {_connect_src()}; "
        "object-src 'none'; "
        "base-uri 'none'; "
        "form-action 'self'; "
        f"frame-ancestors {frame}"
    )


class SecurityHeadersMiddleware:
    def __init__(self, app) -> None:  # noqa: ANN001
        self.app = app

    async def __call__(self, scope, receive, send) -> None:  # noqa: ANN001
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path = str(scope.get("path") or "")
        is_customer = path == "/customer" or path.startswith("/customer/") \
            or path.endswith("/customer")
        s = get_settings()

        async def send_wrapper(message) -> None:  # noqa: ANN001
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("Content-Security-Policy", _csp(is_customer))
                headers.setdefault("X-Content-Type-Options", "nosniff")
                headers.setdefault("Referrer-Policy", "no-referrer")
                headers.setdefault(
                    "X-Frame-Options", "SAMEORIGIN" if is_customer else "DENY"
                )
                headers.setdefault(
                    "Permissions-Policy",
                    "camera=(), microphone=(), geolocation=()",
                )
                headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
                if s.env == "prod":
                    headers.setdefault(
                        "Strict-Transport-Security",
                        "max-age=31536000; includeSubDomains",
                    )
            await send(message)

        await self.app(scope, receive, send_wrapper)
