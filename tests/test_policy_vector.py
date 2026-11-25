"""pgvector 集成测试：仅在设置了 POLICY_VECTOR_TEST_DSN 时运行。

示例：
  POLICY_VECTOR_TEST_DSN=postgresql://agentdock:agentdock@127.0.0.1:5433/agentdock \
  EMBEDDING_PROVIDER=fake uv run pytest tests/test_policy_vector.py -q
"""

import os
from datetime import date, timedelta

import pytest

from domains.policy import embedding, vector_index


@pytest.fixture()
def vector_env(monkeypatch):
    dsn = os.environ.get("POLICY_VECTOR_TEST_DSN", "").strip()
    if not dsn:
        pytest.skip("未设置 POLICY_VECTOR_TEST_DSN，跳过 pgvector 集成测试")
    monkeypatch.setenv("POLICY_VECTOR_DSN", dsn)
    monkeypatch.setenv("EMBEDDING_PROVIDER", "fake")
    monkeypatch.setenv("EMBEDDING_DIM", "512")
    monkeypatch.setenv("EMBEDDING_MODEL", "fake-test")
    from shared.config import get_settings

    get_settings.cache_clear()
    if not vector_index.ping():
        pytest.skip("pgvector 不可达，跳过集成测试")
    vector_index.clear_tenant("t_vec")
    vector_index.clear_tenant("t_vec_other")
    yield
    vector_index.clear_tenant("t_vec")
    vector_index.clear_tenant("t_vec_other")
    get_settings.cache_clear()


def _chunk(text: str) -> list[dict]:
    return [{"chunk_index": 0, "text": text, "content_hash": text}]


def _upsert(tenant: str, clause: str, version: str, text: str,
            start: str, end: str | None = None) -> None:
    vec = embedding.embed_texts([text])[0]
    vector_index.upsert_clause(
        tenant, clause, version, _chunk(text), [vec],
        model_name="fake-test", title=text, keywords="",
        effective_from=start, effective_to=end,
    )


def test_vector_upsert_search_and_isolation(vector_env):
    today = date.today()
    _upsert("t_vec", "CLS-001", "v1", "整机保修 12 个月",
            "2024-01-01")
    _upsert("t_vec_other", "CLS-001", "v1", "整机保修 12 个月",
            "2024-01-01")
    # 过期版本：不得被检索到
    _upsert("t_vec", "CLS-002", "v1", "旧版安装服务已过期",
            "2020-01-01", "2020-12-31")

    q = embedding.embed_texts(["整机保修 12 个月"])[0]
    hits = vector_index.search(q, "t_vec", limit=10)
    assert hits
    assert hits[0]["clause_id"] == "CLS-001"
    assert all(h["clause_id"] != "CLS-002" for h in hits)
    # 租户隔离：另一个租户查不到 t_vec 的 chunk
    other = vector_index.search(q, "t_vec_other", limit=10)
    assert other
    assert vector_index.status("t_vec")["chunks"] == 2
    assert vector_index.status("t_vec_other")["chunks"] == 1


def test_upsert_is_idempotent(vector_env):
    start = (date.today() - timedelta(days=1)).isoformat()
    _upsert("t_vec", "CLS-003", "v1", "发票补开流程", start)
    _upsert("t_vec", "CLS-003", "v1", "发票补开流程（更新）", start)
    assert vector_index.status("t_vec")["chunks"] == 1


def test_fake_embedding_is_deterministic(monkeypatch):
    monkeypatch.setenv("EMBEDDING_PROVIDER", "fake")
    monkeypatch.setenv("EMBEDDING_DIM", "512")
    from shared.config import get_settings

    get_settings.cache_clear()
    try:
        a = embedding.embed_texts(["七天无理由退货"])[0]
        b = embedding.embed_texts(["七天无理由退货"])[0]
        assert a == b
        assert len(a) == int(get_settings().embedding_dim)
    finally:
        get_settings.cache_clear()
