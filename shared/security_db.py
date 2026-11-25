"""共享安全存储（审计 / 审批）：SQLite 开发态，生产换 Postgres。

所有服务进程通过 `SHARED_DB_PATH` 指向同一文件；Docker 部署需挂同一卷。
SQLite 写用进程内 RLock 串行化；多实例生产必须换数据库 advisory lock/队列。
"""

from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None


def db_path() -> str:
    return os.environ.get("SHARED_DB_PATH", ".data/shared.db")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        path = db_path()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(path, check_same_thread=False, timeout=10)
        _conn.row_factory = sqlite3.Row
        with _conn:
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.execute("PRAGMA busy_timeout=10000")
    return _conn


def reset_for_tests(path: str | None = None) -> None:
    """测试隔离用：切库并重建连接。"""
    global _conn
    if path:
        os.environ["SHARED_DB_PATH"] = path
    with _lock:
        if _conn is not None:
            try:
                _conn.close()
            except Exception:  # noqa: BLE001
                pass
        _conn = None
