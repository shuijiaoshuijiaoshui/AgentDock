"""a2a-sdk 1.2.x 接口面 spike：验证 Server 组装 / 客户端调用 / Task 生命周期。

跑法：uv run python examples/spike_a2a_sdk.py
预期输出：echo agent 收到消息 → working → completed + Artifact；客户端逐事件打印。
"""

import asyncio
import threading
import time

import httpx
import uvicorn
from starlette.applications import Starlette

from a2a.client import ClientConfig, ClientFactory
from a2a.helpers import get_message_text, new_data_artifact, new_text_message
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes.agent_card_routes import create_agent_card_routes
from a2a.server.routes.jsonrpc_routes import create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import AgentCapabilities, AgentCard, AgentSkill
from a2a.types.a2a_pb2 import SendMessageRequest

PORT = 8399
BASE = f"http://127.0.0.1:{PORT}"


class EchoExecutor(AgentExecutor):
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.submit()
        await updater.start_work()
        text = context.get_user_input() or "(empty)"
        await updater.add_artifact(
            [new_data_artifact("echo_result", {"echo": text, "ok": True}, name="echo_result")]
        )
        await updater.complete()

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        await TaskUpdater(event_queue, context.task_id, context.context_id).cancel()


async def build_server() -> Starlette:
    card = AgentCard(
        name="Echo Agent",
        url=BASE + "/",
        version="0.0.1",
        capabilities=AgentCapabilities(streaming=True),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        skills=[AgentSkill(id="echo", name="Echo", tags=["echo"])],
    )
    handler = DefaultRequestHandler(
        agent_executor=EchoExecutor(),
        task_store=InMemoryTaskStore(),
        agent_card=card,
    )
    routes = create_jsonrpc_routes(handler, rpc_url="/") + create_agent_card_routes(card)
    return Starlette(routes=routes)


def run_server() -> None:
    app = asyncio.run(build_server())
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")


async def main() -> None:
    t = threading.Thread(target=run_server, daemon=True)
    t.start()
    for _ in range(50):  # 等服务就绪
        try:
            async with httpx.AsyncClient() as hc:
                await hc.get(BASE + "/.well-known/agent-card.json")
            break
        except Exception:
            time.sleep(0.2)

    factory = ClientFactory(ClientConfig(httpx_client=httpx.AsyncClient()))
    client = await factory.create_from_url(BASE + "/")

    req = SendMessageRequest(request=new_text_message("你好，echo！"))
    async for response in client.send_message(req):
        print("EVENT:", type(response).__name__)
        print(response)
    print("SPIKE OK")


if __name__ == "__main__":
    asyncio.run(main())
