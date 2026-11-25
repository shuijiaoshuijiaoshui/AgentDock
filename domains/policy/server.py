"""政策域 Server 入口（:8306）——双形态：A2A Server + /internal 只读 API。

uvicorn domains.policy.server:app --port 8306
"""

import logging
from contextlib import asynccontextmanager

from starlette.applications import Starlette

from domains.policy import graph as domain_graph
from domains.policy.card import build_card
from domains.policy.internal_api import INTERNAL_ROUTES
from shared.a2a_langgraph import (
    LangGraphTaskExecutor,
    build_a2a_app,
    build_checkpointer_cm,
    build_task_store,
)
from shared.auth import build_auth_middleware
from shared.config import get_settings

logger = logging.getLogger(__name__)

DOMAIN = "policy"

executor = LangGraphTaskExecutor(None, domain=DOMAIN,
                                 artifact_builder=domain_graph.artifact_builder)


@asynccontextmanager
async def lifespan(app: Starlette):  # noqa: ANN201
    async with build_checkpointer_cm() as checkpointer:
        await checkpointer.setup()
        executor.bind(domain_graph.build_domain_graph(checkpointer=checkpointer))
        logger.info("%s 域图已就绪", DOMAIN)
        yield


def create_app() -> Starlette:  # noqa: ANN201
    s = get_settings()
    app = build_a2a_app(build_card(s.my_url(s.policy_port)), executor, build_task_store())
    # 双形态：内部只读 API 与 A2A 同进程（/internal/* 受同一内部 token 中间件保护）
    app.router.routes.extend(INTERNAL_ROUTES)
    app.router.lifespan_context = lifespan
    app.add_middleware(build_auth_middleware())
    return app


app = create_app()
