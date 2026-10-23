"""Interop: the A2A client against the official a2a-sdk 1.x server.

Skipped where ``a2a-sdk[http-server]`` is not installed; the client itself
never imports the SDK.
"""

from __future__ import annotations

import asyncio

import pytest

httpx2 = pytest.importorskip("httpx2")
pytest.importorskip("a2a.server.routes.jsonrpc_dispatcher")

from a2a.helpers.proto_helpers import new_task_from_user_message  # noqa: E402
from a2a.server.agent_execution import AgentExecutor, RequestContext  # noqa: E402
from a2a.server.events import EventQueue  # noqa: E402
from a2a.server.request_handlers import DefaultRequestHandler  # noqa: E402
from a2a.server.routes.jsonrpc_dispatcher import JsonRpcDispatcher  # noqa: E402
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater  # noqa: E402
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill, Part  # noqa: E402
from google.protobuf.json_format import MessageToDict  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.responses import JSONResponse  # noqa: E402
from starlette.routing import Route  # noqa: E402

from app_foundation.a2a import A2AClient  # noqa: E402
from app_foundation.net import OutboundEndpointPolicy  # noqa: E402

BASE = "https://peer.example/a2a"


class _Executor(AgentExecutor):
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        if context.current_task is None:
            await event_queue.enqueue_event(new_task_from_user_message(context.message))
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.start_work()
        await updater.add_artifact([Part(text=f"echo: {context.get_user_input()}")], name="result")
        await updater.complete()

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        await TaskUpdater(event_queue, context.task_id, context.context_id).cancel()


def _app() -> Starlette:
    card = AgentCard(
        name="peer",
        description="interop peer",
        version="0.0.1",
        supported_interfaces=[AgentInterface(url=f"{BASE}/rpc", protocol_binding="JSONRPC", protocol_version="1.0")],
        capabilities=AgentCapabilities(streaming=False),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        skills=[AgentSkill(id="echo", name="Echo", description="Echo the input", tags=["test"])],
    )
    handler = DefaultRequestHandler(agent_executor=_Executor(), task_store=InMemoryTaskStore(), agent_card=card)
    dispatcher = JsonRpcDispatcher(request_handler=handler)

    async def card_route(request):
        return JSONResponse(MessageToDict(card))

    return Starlette(
        routes=[
            Route("/a2a/agent-card.json", card_route, methods=["GET"]),
            Route("/a2a/rpc", dispatcher.handle_requests, methods=["POST"]),
        ]
    )


async def _public(host: str, port: int) -> list[str]:
    del host, port
    return ["93.184.216.34"]


@pytest.mark.asyncio
async def test_client_round_trips_with_the_official_sdk_server() -> None:
    client = A2AClient(
        endpoint_policy=OutboundEndpointPolicy(resolver=_public),
        transport=httpx2.ASGITransport(app=_app()),
    )
    card = await client.fetch_card(f"{BASE}/agent-card.json")
    assert [skill.skill_id for skill in card.skills] == ["echo"]

    sent = await client.send_message(card, headers={"Authorization": "Bearer t"}, text="hello")
    assert sent.task_id and sent.context_id

    # SendMessage returns immediately; the executor finishes in the background.
    for _ in range(50):
        got = await client.get_task(card, headers={"Authorization": "Bearer t"}, task_id=sent.task_id)
        if got.terminal:
            break
        await asyncio.sleep(0.02)
    assert got.state == "TASK_STATE_COMPLETED"
    assert got.artifacts[0].parts[0].text == "echo: hello"
