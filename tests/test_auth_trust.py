"""可信身份：只认验签 token，不认 metadata.auth 自称 claim。"""

from types import SimpleNamespace

import pytest
from langchain_core.messages import HumanMessage

import shared.auth as authmod
from orchestrator.graph import intake
from shared import security_db
from shared.auth import (
    AuthError,
    auth_config,
    auth_from_config,
    client_headers,
    create_token,
    csrf_cookie_name,
    decode_action_token,
    decode_token,
    session_cookie_name,
    reset_current_auth,
    set_current_auth,
)
from shared.tenant import tenant_id


@pytest.fixture(autouse=True)
def _reset_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("SHARED_DB_PATH", str(tmp_path / "shared.db"))
    security_db.reset_for_tests(str(tmp_path / "shared.db"))
    set_current_auth(None)
    yield
    set_current_auth(None)
    security_db.reset_for_tests()


def test_metadata_auth_dict_is_rejected():
    cfg = {
        "metadata": {
            "auth": {
                "tenant_id": "t_demo",
                "actor_id": "attacker",
                "role": "service",
                "scopes": ["*"],
            }
        }
    }
    assert auth_from_config(cfg) is None


def test_signed_token_is_accepted():
    token = create_token(
        tenant_id="t_demo", actor_id="CU-1001", role="customer",
        scopes=["order.read"], token_type="user",
    )
    ctx = auth_from_config({"metadata": {"auth_token": token}})
    assert ctx is not None
    assert ctx.tenant_id == "t_demo" and ctx.actor_id == "CU-1001"
    assert ctx.has_scope("order.read")


def test_current_auth_context_is_accepted():
    token = create_token(
        tenant_id="t_demo", actor_id="CU-1001", role="customer",
        scopes=["order.read"],
    )
    ctx = decode_token(token)
    tok = set_current_auth(ctx)
    try:
        assert auth_from_config({}) is ctx
    finally:
        reset_current_auth(tok)


def test_tenant_id_fails_closed_without_auth():
    with pytest.raises(AuthError):
        tenant_id({})


def test_auth_config_is_test_only(monkeypatch):
    monkeypatch.setattr(authmod, "get_settings", lambda: SimpleNamespace(env="dev"))
    with pytest.raises(AuthError):
        auth_config("order.read")


def test_client_headers_fails_closed_outside_test(monkeypatch):
    monkeypatch.setattr(authmod, "get_settings", lambda: SimpleNamespace(env="dev"))
    with pytest.raises(AuthError):
        client_headers(None)


def test_client_headers_test_fallback_is_signed(monkeypatch):
    monkeypatch.setattr(
        authmod, "get_settings",
        lambda: SimpleNamespace(
            env="test", auth_token_ttl_minutes=60, auth_issuer="agentdock",
        ),
    )
    headers = client_headers(None)
    token = headers["Authorization"].split(" ", 1)[1]
    ctx = decode_token(token)
    assert ctx.role == "service" and ctx.tenant_id == "t_demo"


def test_audience_is_verified_when_requested():
    token = create_token(
        tenant_id="t_demo", actor_id="svc", role="service",
        scopes=["security.write"], audience="securityd",
    )
    assert decode_token(token, expected_audience="securityd").actor_id == "svc"
    with pytest.raises(AuthError):
        decode_token(token, expected_audience="other")


# ---------------------------------------------------------------------------
# action token（forced intent 的唯一入口）
# ---------------------------------------------------------------------------

def _action_token(actor: str = "CU-1001", *, audience: str = "orchestrator",
                  typ: str = "action", conversation: str = "conv-1",
                  intent: str = "order_query") -> str:
    return create_token(
        tenant_id="t_demo", actor_id=actor, role="customer",
        scopes=["order.read"], audience=audience, token_type=typ,
        extra_claims={
            "conversation_id": conversation,
            "action_id": "act-1",
            "intent": intent,
            "slots": {"order_no": "SO-2001"},
        },
    )


def test_prod_cookie_names_are_secure_and_separate(monkeypatch):
    monkeypatch.setattr(authmod, "get_settings", lambda: SimpleNamespace(env="prod"))
    assert session_cookie_name("customer") == "__Secure-agentdock_customer_session"
    assert session_cookie_name("workbench") == "__Secure-agentdock_workbench_session"
    assert csrf_cookie_name("customer") == "__Secure-agentdock_customer_csrf"
    assert csrf_cookie_name("workbench") == "__Secure-agentdock_workbench_csrf"


def test_action_token_roundtrip():
    claims = decode_action_token(_action_token())
    assert claims is not None
    assert claims["intent"] == "order_query"
    assert claims["conversation_id"] == "conv-1"
    assert claims["sub"] == "CU-1001"


def test_action_token_tampered_claim_rejected():
    token = _action_token()
    head, body, sig = token.split(".")
    # 篡改 payload（换个合法 b64 字符）：签名必须失败
    tampered_body = ("A" if body[0] != "A" else "B") + body[1:]
    assert decode_action_token(f"{head}.{tampered_body}.{sig}") is None
    # 整体换号（另一 actor 的合法 token）也不该命中同一 action
    assert decode_action_token(_action_token(actor="CU-1002")) is not None


def test_action_token_wrong_audience_or_type_rejected():
    assert decode_action_token(
        _action_token(audience="securityd")) is None
    assert decode_action_token(_action_token(typ="user")) is None


def _intake_cfg(actor: str = "CU-1001", extra: dict | None = None) -> dict:
    cfg = auth_config("order.read", "finance.write", role="customer",
                      actor_id=actor)
    cfg["metadata"].update({
        "context_id": "c1", "trace_id": "t1", "conversation_id": "conv-1",
    })
    if extra:
        cfg["metadata"].update(extra)
    return cfg


def test_intake_ignores_unsigned_action_metadata():
    cfg = _intake_cfg(extra={
        "action": {"intent": "refund_settle", "slots": {"order_no": "SO-2001"}},
    })
    out = intake({"messages": [HumanMessage(content="你好")]}, cfg)
    assert out["forced_intent"] == ""


def test_intake_accepts_signed_action_token():
    token = _action_token()
    cfg = _intake_cfg(extra={"action_token": token})
    out = intake({"messages": [HumanMessage(content="你好")]}, cfg)
    assert out["forced_intent"] == "order_query"
    assert out["forced_slots"] == {"order_no": "SO-2001"}


def test_intake_rejects_action_token_actor_mismatch():
    token = _action_token(actor="CU-1002")
    cfg = _intake_cfg(actor="CU-1001", extra={"action_token": token})
    out = intake({"messages": [HumanMessage(content="你好")]}, cfg)
    assert out["forced_intent"] == ""


def test_intake_rejects_action_token_conversation_mismatch():
    token = _action_token(conversation="conv-1")
    cfg = _intake_cfg(extra={"action_token": token, "conversation_id": "conv-2"})
    out = intake({"messages": [HumanMessage(content="你好")]}, cfg)
    assert out["forced_intent"] == ""


def test_intake_rejects_action_token_unknown_intent():
    token = _action_token(intent="not_a_real_intent")
    cfg = _intake_cfg(extra={"action_token": token})
    out = intake({"messages": [HumanMessage(content="你好")]}, cfg)
    assert out["forced_intent"] == ""
