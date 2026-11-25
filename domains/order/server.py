"""订单域 Server 入口（:8302）。

uvicorn domains.order.server:app --port 8302
"""

import logging
from contextlib import asynccontextmanager

from starlette.applications import Starlette

from domains.order import graph as domain_graph
from domains.order.card import build_card
from shared.a2a_langgraph import (
    LangGraphTaskExecutor,
    build_a2a_app,
    build_checkpointer_cm,
    build_task_store,
)
from shared.auth import build_auth_middleware
from shared.config import get_settings

logger = logging.getLogger(__name__)

DOMAIN = "order"

executor = LangGraphTaskExecutor(
    None, domain=DOMAIN, artifact_builder=domain_graph.artifact_builder,
)


@asynccontextmanager
async def lifespan(app: Starlette):  # noqa: ANN201
    async with build_checkpointer_cm() as checkpointer:
        await checkpointer.setup()
        executor.bind(domain_graph.build_domain_graph(checkpointer=checkpointer))
        logger.info("%s 域图已就绪", DOMAIN)
        yield


def create_app() -> Starlette:  # noqa: ANN201
    s = get_settings()
    url = s.my_url(s.order_port)
    app = build_a2a_app(build_card(url), executor, build_task_store())
    app.router.lifespan_context = lifespan
    app.add_middleware(build_auth_middleware())
    return app


app = create_app()
