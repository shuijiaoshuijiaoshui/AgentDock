"""前端安全：无动态 innerHTML / 无内联事件与内联样式 / CSP 收紧 / 无 localStorage。"""

from pathlib import Path

import httpx
import pytest

from console.server import app

STATIC = Path(__file__).resolve().parents[1] / "console" / "static"
INLINE_HANDLERS = ("onclick=", "onload=", "onerror=", "onkeydown=")
JS_FILES = (
    "customer.js",
    "workbench.js",
    "portal.js",
    "js/api.js",
    "js/components.js",
    "js/ws.js",
)


def test_no_inline_handlers_or_inline_scripts():
    for name in ("customer.html", "workbench.html", "portal.html"):
        html = (STATIC / name).read_text(encoding="utf-8")
        for token in INLINE_HANDLERS:
            assert token not in html, f"{name} 仍有内联事件 {token}"
        # 只允许带 src 的外置脚本
        for chunk in html.split("<script")[1:]:
            assert "src=" in chunk.split(">", 1)[0], f"{name} 仍有内联脚本"


def test_no_inline_style_attributes_or_blocks():
    """CSP style-src 'self'：不允许 style 属性，也不允许 <style> 块。"""
    for name in ("customer.html", "workbench.html", "portal.html"):
        html = (STATIC / name).read_text(encoding="utf-8")
        assert "<style" not in html, f"{name} 仍有 <style> 块"
        assert "style=" not in html, f"{name} 仍有 style 属性"


def test_no_dynamic_innerhtml_in_frontend_js():
    for name in JS_FILES:
        js = (STATIC / name).read_text(encoding="utf-8")
        assert "innerHTML" not in js, f"{name} 仍使用 innerHTML"
        assert ".style." not in js, f"{name} 仍直接写 style（CSP 会拦截）"
    customer = (STATIC / "customer.js").read_text(encoding="utf-8")
    assert "textContent" in customer


def test_no_localstorage_tokens():
    for name in JS_FILES:
        js = (STATIC / name).read_text(encoding="utf-8")
        assert "localStorage" not in js, f"{name} 仍使用 localStorage"


@pytest.mark.asyncio
async def test_csp_and_security_headers():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test",
    ) as client:
        workbench = await client.get("/")
        customer = await client.get("/customer")
        portal = await client.get("/portal")
    assert workbench.status_code == 200
    assert customer.status_code == 200
    assert portal.status_code == 200
    w_csp = workbench.headers["content-security-policy"]
    c_csp = customer.headers["content-security-policy"]
    p_csp = portal.headers["content-security-policy"]
    for csp in (w_csp, c_csp, p_csp):
        assert "script-src 'self'" in csp
        assert "style-src 'self'" in csp
        assert "unsafe-inline" not in csp
    assert "frame-ancestors 'none'" in w_csp
    assert "frame-ancestors 'self'" in c_csp
    assert workbench.headers["x-content-type-options"] == "nosniff"
    assert workbench.headers["x-frame-options"] == "DENY"
    assert customer.headers["x-frame-options"] == "SAMEORIGIN"


@pytest.mark.asyncio
async def test_portal_embeds_same_origin_customer():
    html = (STATIC / "portal.html").read_text(encoding="utf-8")
    assert 'data-src="customer"' in html
    assert "http://" not in html and "https://" not in html
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test",
    ) as client:
        frame_target = await client.get("/customer")
    assert frame_target.headers["x-frame-options"] == "SAMEORIGIN"


@pytest.mark.asyncio
async def test_static_js_served_without_inline_content():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test",
    ) as client:
        r = await client.get("/static/workbench.js")
    assert r.status_code == 200
    assert "innerHTML" not in r.text


def test_workbench_style_extracted_to_css():
    css = (STATIC / "workbench.css").read_text(encoding="utf-8")
    assert ".wb-main" in css and ".trace-step" in css
    html = (STATIC / "workbench.html").read_text(encoding="utf-8")
    assert "static/workbench.css" in html
