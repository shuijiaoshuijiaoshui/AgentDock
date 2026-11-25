"""spike client：调 spike_server.py，打印事件流。"""

import asyncio

import httpx

from a2a.client import ClientConfig, ClientFactory
from a2a.helpers import new_text_message
from a2a.types.a2a_pb2 import SendMessageRequest

BASE = "http://127.0.0.1:8399"


async def main() -> None:
    async with httpx.AsyncClient(timeout=10) as hc:
        r = await hc.get(BASE + "/.well-known/agent-card.json")
        print("card status:", r.status_code)
        print("card name:", r.json().get("name"))

    factory = ClientFactory(ClientConfig(httpx_client=httpx.AsyncClient(timeout=10)))
    client = await factory.create_from_url(BASE + "/")
    req = SendMessageRequest(message=new_text_message("你好，echo！"))
    async for response in client.send_message(req):
        print("EVENT:", type(response).__name__)
        print(response)
    print("SPIKE OK")


if __name__ == "__main__":
    asyncio.run(main())
