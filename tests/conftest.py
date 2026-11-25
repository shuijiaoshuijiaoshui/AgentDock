import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 测试进程使用 ENV=test：仅此环境允许 auth_config 测试 helper 与无身份
# client_headers 兜底；dev/prod 必须显式传验签 token。
os.environ.setdefault("ENV", "test")
os.environ.setdefault("AUTH_SECRET", "test-secret")

from shared.config import get_settings  # noqa: E402

get_settings.cache_clear()
