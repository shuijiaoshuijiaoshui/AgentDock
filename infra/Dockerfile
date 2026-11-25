# AgentDock 基础镜像：uv 管理依赖（与 lockfile 一致）
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY shared ./shared
COPY orchestrator ./orchestrator
COPY domains ./domains
COPY console ./console
COPY security_service ./security_service
COPY scripts ./scripts
COPY infra/mockdata ./infra/mockdata

# 构建期烘焙 Mock 数据（不依赖构建机本地文件；重跑 seed 幂等）
RUN uv run python scripts/seed_mock.py

ENV PYTHONUNBUFFERED=1
# SERVICE_TARGET 如 orchestrator.server:app；SERVICE_PORT 如 8300；
# PUBLIC_URL 必须是别的容器可达的地址（如 http://orchestrator:8300）——
# a2a-sdk 客户端会跟随 AgentCard.url 回连，127.0.0.1 在容器间不可达
CMD ["sh", "-c", "uv run uvicorn ${SERVICE_TARGET} --host 0.0.0.0 --port ${SERVICE_PORT}"]
