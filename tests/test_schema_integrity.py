"""Schema 完整性：所有业务表的 PK/唯一索引必须含 tenant_id（PRAGMA 断言）。

不靠人工检查：从 sqlite_master 枚举表，逐表读 PRAGMA table_info/index_list/
index_info；新增表若漏 tenant 键，本测试直接失败。
"""

import importlib
import sqlite3

import pytest

from scripts import seed_mock


def _tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return sorted(r[0] for r in rows)


def _pk_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})") if r[5]]


def _unique_indexes(conn: sqlite3.Connection, table: str) -> list[list[str]]:
    out: list[list[str]] = []
    for idx in conn.execute(f"PRAGMA index_list({table})"):
        # idx = (seq, name, unique, origin, partial)
        if not idx[2]:
            continue
        cols = [r[2] for r in conn.execute(f"PRAGMA index_info({idx[1]})")]
        out.append(cols)
    return out


@pytest.fixture()
def business_db(tmp_path, monkeypatch):
    path = tmp_path / "business.db"
    monkeypatch.setattr(seed_mock, "DB", path)
    seed_mock.seed()
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn, path
    finally:
        conn.close()


def test_every_business_table_has_tenant_in_pk_or_unique(business_db):
    conn, _ = business_db
    tables = _tables(conn)
    assert "orders" in tables and "policy_docs" in tables
    for table in tables:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        assert "tenant_id" in cols, f"{table} 缺少 tenant_id 列"
        pk = _pk_columns(conn, table)
        assert pk, f"{table} 没有主键"
        assert "tenant_id" in pk, f"{table} 主键不含 tenant_id: {pk}"
        for cols_in_index in _unique_indexes(conn, table):
            assert "tenant_id" in cols_in_index, (
                f"{table} 的唯一索引不含 tenant_id: {cols_in_index}"
            )


def test_required_business_tables_are_covered(business_db):
    conn, _ = business_db
    required = {
        "products", "promos", "coupons", "customers", "customer_profiles",
        "policy_docs", "knowledge_entries", "aftersales", "tickets", "orders",
        "order_items", "shipments", "shipment_nodes", "refunds", "invoices",
        "return_orders",
        "order_cancel_log", "reconciliations", "complaints",
        # 营销经营分析（marketing 域）
        "ad_campaigns", "sales_daily", "competitor_prices", "marketing_plans",
        "subsidies",
    }
    assert required <= set(_tables(conn))


def test_same_business_ids_are_isolated_per_tenant(business_db):
    conn, _ = business_db
    rows = conn.execute(
        "SELECT tenant_id, order_no, customer_id, amount FROM orders "
        "WHERE order_no='SO-2001' ORDER BY tenant_id"
    ).fetchall()
    assert len(rows) == 2
    assert {r["tenant_id"] for r in rows} == {"t_demo", "t_other"}
    assert {r["customer_id"] for r in rows} == {"CU-1001", "CU-2001"}
    assert rows[0]["amount"] != rows[1]["amount"]

    skus = conn.execute(
        "SELECT tenant_id FROM products WHERE sku='SKU-A001' ORDER BY tenant_id"
    ).fetchall()
    assert [r["tenant_id"] for r in skus] == ["t_demo", "t_other"]

    # 同租户内重复业务键必须被主键拒绝
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO orders(tenant_id, order_no, customer_id, sku, qty, amount, "
            "status) VALUES ('t_demo','SO-2001','CU-1001','SKU-A001',1,1.0,'paid')"
        )


def test_a2a_tasks_composite_key(tmp_path):
    from shared.a2a_langgraph import SqliteTaskStore

    store = SqliteTaskStore(str(tmp_path / "tasks.sqlite"))
    conn = sqlite3.connect(tmp_path / "tasks.sqlite")
    try:
        pk = _pk_columns(conn, "a2a_tasks")
        assert "tenant_id" in pk and "task_id" in pk
        for cols_in_index in _unique_indexes(conn, "a2a_tasks"):
            assert "tenant_id" in cols_in_index
        # 同一 task_id 可以在两个租户各存一份
        conn.execute(
            "INSERT INTO a2a_tasks(tenant_id, task_id, context_id, state, payload) "
            "VALUES ('t_demo','T-SAME','C1','x','{}')"
        )
        conn.execute(
            "INSERT INTO a2a_tasks(tenant_id, task_id, context_id, state, payload) "
            "VALUES ('t_other','T-SAME','C2','x','{}')"
        )
        conn.commit()
        assert conn.execute(
            "SELECT count(*) FROM a2a_tasks WHERE task_id='T-SAME'"
        ).fetchone()[0] == 2
    finally:
        conn.close()
        store._conn.close()


def test_a2a_tasks_legacy_single_pk_is_migrated(tmp_path):
    from shared.a2a_langgraph import SqliteTaskStore

    path = tmp_path / "legacy.sqlite"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE a2a_tasks ("
        "tenant_id TEXT NOT NULL DEFAULT 't_demo',"
        "task_id TEXT PRIMARY KEY,"
        "context_id TEXT NOT NULL,"
        "state TEXT NOT NULL,"
        "payload TEXT NOT NULL,"
        "updated_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    conn.execute(
        "INSERT INTO a2a_tasks(tenant_id, task_id, context_id, state, payload) "
        "VALUES ('t_demo','T-LEGACY','C1','x','{}')"
    )
    conn.commit()
    conn.close()

    store = SqliteTaskStore(str(path))
    conn = sqlite3.connect(path)
    try:
        assert _pk_columns(conn, "a2a_tasks") == ["tenant_id", "task_id"]
        assert conn.execute(
            "SELECT count(*) FROM a2a_tasks WHERE task_id='T-LEGACY'"
        ).fetchone()[0] == 1
    finally:
        conn.close()
        store._conn.close()

