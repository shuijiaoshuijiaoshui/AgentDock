"""登录页元数据门禁：只有 ENV=dev 且 DEV_MODE=true 才返回演示口令。"""

import json
from types import SimpleNamespace

import pytest

import console.server as server


def _body(resp) -> dict:  # noqa: ANN001
    return json.loads(resp.body.decode("utf-8"))


@pytest.mark.asyncio
async def test_dev_mode_returns_demo_accounts(monkeypatch):
    monkeypatch.setattr(
        server, "get_settings",
        lambda: SimpleNamespace(env="dev", dev_mode=True),
    )
    monkeypatch.setattr(server.db, "list_tenants", lambda: [{"tenant_id": "t_demo"}])
    body = _body(await server.api_auth_config(None))
    assert body["demo_password"] == "demo123"
    assert body["demo_accounts"]


@pytest.mark.asyncio
async def test_test_env_does_not_echo_demo_password(monkeypatch):
    monkeypatch.setattr(
        server, "get_settings",
        lambda: SimpleNamespace(env="test", dev_mode=True),
    )
    monkeypatch.setattr(server.db, "list_tenants", lambda: [{"tenant_id": "t_demo"}])
    body = _body(await server.api_auth_config(None))
    assert "demo_password" not in body
    assert "demo_accounts" not in body


@pytest.mark.asyncio
async def test_dev_env_without_dev_mode_does_not_echo_demo(monkeypatch):
    monkeypatch.setattr(
        server, "get_settings",
        lambda: SimpleNamespace(env="dev", dev_mode=False),
    )
    monkeypatch.setattr(server.db, "list_tenants", lambda: [{"tenant_id": "t_demo"}])
    body = _body(await server.api_auth_config(None))
    assert "demo_password" not in body
    assert body["tenants"]


@pytest.mark.asyncio
async def test_prod_does_not_return_tenants(monkeypatch):
    monkeypatch.setattr(
        server, "get_settings",
        lambda: SimpleNamespace(env="prod", dev_mode=False),
    )
    monkeypatch.setattr(server.db, "list_tenants", lambda: [{"tenant_id": "t_demo"}])
    body = _body(await server.api_auth_config(None))
    assert "tenants" not in body
    assert "demo_password" not in body

