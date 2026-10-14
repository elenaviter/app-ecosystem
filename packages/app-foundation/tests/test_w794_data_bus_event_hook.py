"""W794: a keyed push hook sees each Data Bus event as it arrives, beside the queue."""

from __future__ import annotations

import asyncio

from app_foundation.data_bus.client import FederatedDataBusClient as DataBusClient


def test_a_keyed_hook_sees_each_event_and_never_blocks_delivery():
    async def scenario():
        client = DataBusClient.__new__(DataBusClient)
        client._events = asyncio.Queue(maxsize=4)  # noqa: SLF001
        client._event_hooks = {}  # noqa: SLF001
        seen: list[str] = []
        client.set_event_hook("a", lambda event: seen.append("first:" + event["kind"]))
        client.set_event_hook("a", lambda event: seen.append("a:" + event["kind"]))
        client.set_event_hook("b", lambda event: (_ for _ in ()).throw(RuntimeError("broken hook")))
        client._queue_event({"kind": "control.available"})  # noqa: SLF001
        assert seen == ["a:control.available"]
        assert client._events.get_nowait()["kind"] == "control.available"  # noqa: SLF001 - delivered despite the failing hook
        client.set_event_hook("a", None)
        client._queue_event({"kind": "x"})  # noqa: SLF001
        assert seen == ["a:control.available"]

    asyncio.run(scenario())
