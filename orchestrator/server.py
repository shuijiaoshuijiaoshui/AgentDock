"""编排 Server 入口（:8300）——一主。

uvicorn orchestrator.server:app --port 8300
"""

import logging
from contextlib import asynccontextmanager

from starlette.applications import Starlette

from orchestrator import graph as orch_graph
from shared.a2a_langgraph import (
    LangGraphTaskExecutor,
    build_a2a_app,
    build_agent_card,
    build_checkpointer_cm,
    build_task_store,
)
from shared.auth import build_auth_middleware
from shared.config import get_settings

logger = logging.getLogger(__name__)

executor = LangGraphTaskExecutor(
    None, domain="orchestrator", artifact_builder=orch_graph.orchestrator_artifact_builder
)


@asynccontextmanager
async def lifespan(app: Starlette):  # noqa: ANN201
    async with build_checkpointer_cm() as checkpointer:
        await checkpointer.setup()
        executor.bind(orch_graph.build_orchestrator_graph(checkpointer=checkpointer))
        logger.info("编排主图已就绪")
        yield


def create_app() -> Starlette:  # noqa: ANN201
    s = get_settings()
    card = build_agent_card(
        name="商家经营助手（编排）",
        description="AgentDock 一主：意图理解、确定性路由、域委托与答案合成",
        url=s.my_url(s.orchestrator_port),
        skills=[
            {
                "id": "orchestrate",
                "name": "商家经营问答",
                "description": "接收自然语言请求，路由到域 Agent 并合成回答",
                "tags": ["orchestrator"],
            }
        ],
    )
    app = build_a2a_app(card, executor, build_task_store())
    app.router.lifespan_context = lifespan
    app.add_middleware(build_auth_middleware())
    return app


app = create_app()
