"""spike server：单独起 echo agent，供 spike_client.py 调用。"""

import asyncio

import uvicorn
from starlette.applications import Starlette

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes.agent_card_routes import create_agent_card_routes
from a2a.server.routes.jsonrpc_routes import create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import AgentCapabilities, AgentCard, AgentSkill
from a2a.helpers import new_data_part, new_task
from a2a.utils import TransportProtocol
from a2a.types import TaskState
from a2a.types.a2a_pb2 import AgentInterface


class EchoExecutor(AgentExecutor):
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        print(f"[executor] task={context.task_id} ctx={context.context_id}", flush=True)
        await event_queue.enqueue_event(
            new_task(
                task_id=context.task_id,
                context_id=context.context_id,
                state=TaskState.TASK_STATE_SUBMITTED,
            )
        )
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.start_work()
        text = context.get_user_input() or "(empty)"
        await updater.add_artifact(
            [new_data_part({"echo": text, "ok": True})], name="echo_result"
        )
        await updater.complete()
        print("[executor] completed", flush=True)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        await TaskUpdater(event_queue, context.task_id, context.context_id).cancel()


async def build_app() -> Starlette:
    card = AgentCard(
        name="Echo Agent",
        description="spike echo agent",
        version="0.0.1",
        capabilities=AgentCapabilities(streaming=True),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        skills=[
            AgentSkill(id="echo", name="Echo", description="回声", tags=["echo"])
        ],
        supported_interfaces=[
            AgentInterface(
                url="http://127.0.0.1:8399/",
                protocol_binding=TransportProtocol.JSONRPC,
                protocol_version="1.0",
            )
        ],
    )
    handler = DefaultRequestHandler(
        agent_executor=EchoExecutor(),
        task_store=InMemoryTaskStore(),
        agent_card=card,
    )
    routes = create_jsonrpc_routes(handler, rpc_url="/") + create_agent_card_routes(card)
    return Starlette(routes=routes)


if __name__ == "__main__":
    app = asyncio.run(build_app())
    uvicorn.run(app, host="127.0.0.1", port=8399, log_level="info")
