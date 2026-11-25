"""业务库连接（域与 console 共用同一份 SQLite；生产演进为 Postgres）。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from shared.config import get_settings


def business_db_path() -> Path:
    return Path(get_settings().business_db_path)


def connect_business_db() -> sqlite3.Connection:
    conn = sqlite3.connect(business_db_path())
    conn.row_factory = sqlite3.Row
    return conn
