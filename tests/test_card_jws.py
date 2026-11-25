"""AgentCard JWS 签发/验签（PRD 7.3.1 / M4）。

单测：签发→验签往返、篡改拒绝、未签名拒绝、错钥拒绝、kid 轮换并存、
未知 kid 快速失败、prod 拒绝自动生成、Card 缓存 TTL。
e2e（真进程桩域，签名关闭）：delegate 验签失败 → 拒绝委托 + 审计 card.verify_failed。
"""

import base64
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from a2a.utils.signing import InvalidSignaturesError, NoSignatureError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 测试进程的密钥与审计库与 .env 隔离：先改环境再清 settings 缓存
os.environ["AUTH_SECRET"] = "test-secret"
os.environ.setdefault("ENV", "test")

from shared import card_signing  # noqa: E402
from shared.a2a_client import (  # noqa: E402
    _CARD_CACHE,
    _card_cache_get,
    _card_cache_put,
    delegate,
)
from shared.a2a_langgraph import build_agent_card  # noqa: E402
from shared.auth import (  # noqa: E402
    ALL_SCOPES,
    auth_from_headers,
    create_token,
)
from shared.card_signing import CardVerificationError  # noqa: E402
from shared.config import get_settings  # noqa: E402
from shared.keys import KeyConfigurationError  # noqa: E402


def _card() -> object:
    return build_agent_card(
        name="测试 Agent", description="JWS 测试",
        url="http://127.0.0.1:1/", skills=[
            {"id": "s.x", "name": "x", "description": "x", "tags": ["t"]},
        ],
    )


def _protected_header(signature) -> dict:  # noqa: ANN001
    raw = signature.protected
    padded = raw + "=" * (-len(raw) % 4)
    return json.loads(base64.urlsafe_b64decode(padded))


def _settings(tmp_path: Path, *, key_version: str = "v2", verify_keys=None,
              signing_file: Path | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        env="test", key_version=key_version,
        card_signing_key_file=str(signing_file or tmp_path / "card_signing_key.pem"),
        card_verify_keys_json=json.dumps(verify_keys) if verify_keys else "",
        card_signing_enabled=True, card_verify_enabled=True,
        card_cache_ttl_seconds=300,
    )


def _patch(monkeypatch, st: SimpleNamespace) -> None:
    monkeypatch.setattr(card_signing, "get_settings", lambda: st)


def _pub(tmp_path: Path, name: str) -> str:
    """在独立目录生成一把新钥，返回公钥 PEM 文本（轮换/错钥用例用）。"""
    path = card_signing.ensure_card_key(path=tmp_path / name / "card_signing_key.pem")
    return (path.parent / f"{path.stem}_public.pem").read_text()


def test_sign_and_verify_roundtrip(tmp_path, monkeypatch):
    _patch(monkeypatch, _settings(tmp_path, key_version="v2"))
    signed = card_signing.sign_card(_card())
    assert len(signed.signatures) == 1
    header = _protected_header(signed.signatures[0])
    assert header["kid"] == "v2"
    assert header["alg"] == "ES256"
    card_signing.build_card_verifier()(signed)  # 不抛 = 验签通过


def test_tampered_card_rejected(tmp_path, monkeypatch):
    _patch(monkeypatch, _settings(tmp_path))
    signed = card_signing.sign_card(_card())
    signed.name = "篡改后的 Agent"
    with pytest.raises(InvalidSignaturesError):
        card_signing.build_card_verifier()(signed)


def test_unsigned_card_rejected(tmp_path, monkeypatch):
    _patch(monkeypatch, _settings(tmp_path))
    with pytest.raises(NoSignatureError):
        card_signing.build_card_verifier()(_card())


def test_wrong_key_rejected(tmp_path, monkeypatch):
    other = _pub(tmp_path, "other")
    _patch(monkeypatch, _settings(tmp_path, verify_keys={"v2": other}))
    signed = card_signing.sign_card(_card())  # 用本目录私钥签
    with pytest.raises(InvalidSignaturesError):
        card_signing.build_card_verifier()(signed)


def test_kid_rotation_both_versions_accepted(tmp_path, monkeypatch):
    """v1 旧卡 + v2 新卡并存：验签方按 Card kid 各取各的公钥。"""
    old_pub = _pub(tmp_path, "old")
    old_file = tmp_path / "old" / "card_signing_key.pem"
    _patch(monkeypatch, _settings(tmp_path, key_version="v1", signing_file=old_file))
    old_card = card_signing.sign_card(_card())

    new_file = tmp_path / "new" / "card_signing_key.pem"
    _patch(monkeypatch, _settings(tmp_path, key_version="v2", signing_file=new_file,
                                  verify_keys={"v1": old_pub}))
    new_card = card_signing.sign_card(_card())

    verifier = card_signing.build_card_verifier()
    verifier(old_card)  # 旧 kid 经 CARD_VERIFY_KEYS_JSON
    verifier(new_card)  # 当前 kid 从私钥派生公钥


def test_unknown_kid_fast_fail(tmp_path, monkeypatch):
    _patch(monkeypatch, _settings(tmp_path, key_version="v9"))
    signed = card_signing.sign_card(_card())
    _patch(monkeypatch, _settings(tmp_path, key_version="v2"))
    with pytest.raises(CardVerificationError):
        card_signing.build_card_verifier()(signed)


def test_prod_requires_provisioned_key(tmp_path, monkeypatch):
    st = _settings(tmp_path)
    st.env = "prod"
    _patch(monkeypatch, st)
    with pytest.raises(KeyConfigurationError):
        card_signing.ensure_card_key(path=tmp_path / "missing.pem")


def test_card_cache_hit_and_ttl(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(
        "shared.a2a_client.get_settings",
        lambda: SimpleNamespace(card_cache_ttl_seconds=300),
    )
    _card_cache_put("u1", sentinel)
    assert _card_cache_get("u1") is sentinel

    monkeypatch.setattr(
        "shared.a2a_client.get_settings",
        lambda: SimpleNamespace(card_cache_ttl_seconds=0),
    )
    assert _card_cache_get("u1") is None  # TTL=0 立即过期并清除
    assert "u1" not in _CARD_CACHE


# ---------------------------------------------------------------------------
# e2e：真进程桩域（签名关闭）→ delegate 拒绝并审计
# ---------------------------------------------------------------------------

def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="module")
def unsigned_stub(tmp_path_factory):
    tmp = Path(tempfile.mkdtemp(prefix="card_jws_e2e_"))
    get_settings.cache_clear()
    os.environ["SHARED_DB_PATH"] = str(tmp / "shared.db")
    port = _free_port()
    env = {
        **os.environ,
        "ENV": "test", "DEV_MODE": "true", "AUTH_SECRET": "test-secret",
        "SECURITY_MODE": "local", "SHARED_DB_PATH": str(tmp / "shared.db"),
        "SQLITE_DB_PATH": str(tmp / "stub.sqlite"),
        "STUB_DOMAIN": "order", "ORDER_PORT": str(port),
        "CARD_SIGNING_ENABLED": "false",
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "domains._stub.stub_server:app",
         "--port", str(port), "--log-level", "warning"],
        env=env, cwd=ROOT,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + 40
    while time.time() < deadline:
        try:
            r = httpx.get(
                f"http://127.0.0.1:{port}/.well-known/agent-card.json",
                headers={"Authorization": "Bearer x"}, timeout=2, trust_env=False,
            )
            if r.status_code == 200:
                break
        except Exception:  # noqa: BLE001
            time.sleep(0.4)
    else:
        proc.terminate()
        pytest.fail("unsigned stub 未就绪")
    yield f"http://127.0.0.1:{port}"
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    _CARD_CACHE.clear()


async def test_delegate_rejects_unsigned_card_and_audits(unsigned_stub):
    from shared.audit import list_events

    token = create_token(tenant_id="t_demo", actor_id="tester", role="service",
                         scopes=ALL_SCOPES, token_type="service")
    auth = auth_from_headers({"authorization": f"Bearer {token}"})
    with pytest.raises(CardVerificationError):
        await delegate(unsigned_stub, "你好", auth=auth)

    events = list_events("t_demo", action="card.verify_failed")
    assert any(e.get("target") == unsigned_stub for e in events), events
