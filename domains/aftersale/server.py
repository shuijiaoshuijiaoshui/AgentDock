"""售后域 Server 入口（:8304）。

资格/责任判定内部经 PolicyClient 调政策域 /internal 只读 API（不建 Task）。
uvicorn domains.aftersale.server:app --port 8304
"""

import logging
from contextlib import asynccontextmanager

from starlette.applications import Starlette

from domains.aftersale import graph as domain_graph
from domains.aftersale.card import build_card
from shared.a2a_langgraph import (
    LangGraphTaskExecutor,
    build_a2a_app,
    build_checkpointer_cm,
    build_task_store,
)
from shared.auth import build_auth_middleware
from shared.config import get_settings

logger = logging.getLogger(__name__)

DOMAIN = "aftersale"

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
    app = build_a2a_app(build_card(s.my_url(s.aftersale_port)), executor, build_task_store())
    # 坐席工单 CRUD 走内部 API（逻辑单份在域内，console 只是代理）
    from domains.aftersale.internal_api import INTERNAL_ROUTES

    app.router.routes.extend(INTERNAL_ROUTES)
    app.router.lifespan_context = lifespan
    app.add_middleware(build_auth_middleware())
    return app


app = create_app()
