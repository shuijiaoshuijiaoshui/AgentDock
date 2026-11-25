"""进程内滑动窗口限流（开发/单实例）。

生产多实例必须换 Redis/网关限流；本模块只保证单进程 demo 有第一道闸，
并让限流键包含 tenant + actor，避免“一个租户打满全局限额”。
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

from shared.config import get_settings

_lock = threading.Lock()
_buckets: dict[str, deque[float]] = defaultdict(deque)


def check(key: str, *, limit: int, window_seconds: int = 60) -> tuple[bool, int]:
    """返回 (allowed, retry_after_seconds)。"""
    if not get_settings().rate_limit_enabled:
        return True, 0
    now = time.monotonic()
    cutoff = now - window_seconds
    with _lock:
        q = _buckets[key]
        while q and q[0] < cutoff:
            q.popleft()
        if len(q) >= limit:
            retry = max(1, int(window_seconds - (now - q[0])))
            return False, retry
        q.append(now)
    return True, 0


def check_user(tenant_id: str, actor_id: str, bucket: str = "http") -> tuple[bool, int]:
    s = get_settings()
    limit = s.rate_limit_ws_per_minute if bucket == "ws" else s.rate_limit_per_minute
    return check(f"{tenant_id}:{actor_id}:{bucket}", limit=limit)


def reset() -> None:
    """测试用：清空所有窗口。"""
    with _lock:
        _buckets.clear()
