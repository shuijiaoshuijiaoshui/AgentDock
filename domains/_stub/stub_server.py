"""桩域 Server（技术方案 8 章 M1）：Card 可发现、调用一律返回 rejected。

通过环境变量参数化，一个文件起五个域：
    STUB_DOMAIN=order STUB_SKILLS=order.query,order.amend \
      uvicorn domains._stub.stub_server:app --port 8302
"""

import logging
import os
from contextlib import asynccontextmanager

from starlette.applications import Starlette

from a2a.helpers import new_text_message
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import TaskState

from shared.a2a_langgraph import (
    build_a2a_app,
    build_agent_card,
    build_task_store,
)
from shared.auth import build_auth_middleware
from shared.config import get_settings

logger = logging.getLogger(__name__)


class RejectExecutor(AgentExecutor):
    """桩语义：skill 尚未实现（PRD 12 章 M1）——明确 rejected，不假装成功。"""

    def __init__(self, domain: str) -> None:
        self.domain = domain

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        await event_queue.enqueue_event(
            _new_task(context.task_id, context.context_id)
        )
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.start_work()
        await updater.reject(
            new_text_message(
                f"{self.domain} 域为桩 Server：skill 尚未实现（M3 交付）。",
                task_id=context.task_id,
                context_id=context.context_id,
            )
        )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        await event_queue.enqueue_event(
            _new_task(context.task_id, context.context_id)
        )
        await TaskUpdater(event_queue, context.task_id, context.context_id).cancel()


def _new_task(task_id: str, context_id: str):  # noqa: ANN201
    from a2a.helpers import new_task

    return new_task(
        task_id=task_id,
        context_id=context_id,
        state=TaskState.TASK_STATE_SUBMITTED,
    )


DEFAULT_SKILLS = {
    "order": ["order.query", "order.amend"],
    "fulfillment": ["fulfill.atp", "fulfill.track"],
    "finance": ["billing.statement", "invoice.issue", "refund.status", "refund.settle"],
}


def create_app() -> Starlette:  # noqa: ANN201
    s = get_settings()
    domain = os.environ.get("STUB_DOMAIN", "order")
    skills = os.environ.get("STUB_SKILLS", "").split(",")
    skills = [sk for sk in skills if sk] or DEFAULT_SKILLS.get(domain, [f"{domain}.stub"])
    ports = {
        "order": s.order_port, "fulfillment": s.fulfillment_port,
        "aftersale": s.aftersale_port, "finance": s.finance_port,
        "policy": s.policy_port,
    }
    port = ports.get(domain, 8302)
    card = build_agent_card(
        name=f"{domain} Agent（桩）",
        description=f"{domain} 域桩 Server：Card 可发现，调用返回 rejected（M3 交付真实现）",
        url=s.my_url(port),
        skills=[
            {"id": sk, "name": sk, "description": f"{sk}（M3 桩）", "tags": [domain]}
            for sk in skills
        ],
        streaming=False,
    )
    app = build_a2a_app(card, RejectExecutor(domain), build_task_store())
    app.add_middleware(build_auth_middleware())
    return app


app = create_app()
