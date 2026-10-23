"""W918: a worker reaches the services its Card allows through Connection Hub's gateway."""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from project_board.client.agent_services import (
    CALL_ACTION,
    DESCRIBE_ACTION,
    INVOCATION_META_KEY,
    LIST_ACTION,
    MAX_RESULT_BYTES,
    gateway_url,
    is_agent_services_action,
    run_agent_services,
)
from project_board.client.render import render_envelope
from project_board.contract.errors import DomainError

SEARCH = "ch_remote_mcp_1111111111111111__search_2222222222222222"
BOARD = "ch_catalog_3333333333333333__item_read_4444444444444444"


def _tool(name, *, resource, kind="remote_mcp", operation="search", properties=("query",)):
    return {
        "name": name,
        "title": operation.title(),
        "description": f"{operation} the fixture",
        "inputSchema": {"type": "object", "properties": {key: {"type": "string"} for key in properties},
                        "required": list(properties)},
        "_meta": {"connection_hub": {"resource_id": resource, "resource_kind": kind, "operation": operation}},
    }


TOOLS = [
    _tool(SEARCH, resource="urn:connection-hub:remote-mcp:mcp_aaaaaaaaaaaaaaaaaaaaaaaa"),
    _tool(BOARD, resource="*/api/integrations/bundles/*/*/problem-board@1-0/public/mcp/problem_board*",
          kind="catalog", operation="item_read"),
    {"name": "connection_hub_access_describe", "title": "Describe", "inputSchema": {"type": "object"}},
]


class _Remote:
    def __init__(self, result=None):
        self.calls = []
        self.result = result or {"content": [{"type": "text", "text": "ok"}], "structuredContent": {"hits": 1},
                                  "_meta": {"connection_hub": {"invocation_id": "inv-1", "replay": False}}}

    async def list_tools(self, *, params=None):
        return {"tools": TOOLS}

    async def call_tool(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


def _opener(remote):
    @asynccontextmanager
    async def open_tools():
        yield remote, object()

    return open_tools


def test_the_gateway_url_is_connection_hubs_on_the_boards_platform():
    assert gateway_url(
        "https://host.example/api/integrations/bundles/t/p/problem-board@1-0/public/mcp/problem_board", "t", "p",
    ) == "https://host.example/api/integrations/bundles/t/p/connection-hub@1-0/public/mcp/delegated_mcp_gateway"
    assert is_agent_services_action(LIST_ACTION) and not is_agent_services_action("project.plan.item")


@pytest.mark.asyncio
async def test_the_list_hides_the_boards_own_surface_and_the_access_meta_tool():
    result = await run_agent_services(LIST_ACTION, {}, open_tools=_opener(_Remote()))
    assert [row["name"] for row in result["services"]] == [SEARCH]
    row = result["services"][0]
    assert row["resource_kind"] == "remote_mcp" and row["operation"] == "search"
    assert row["arguments"] == ["query"] and row["required"] == ["query"]


@pytest.mark.asyncio
async def test_describe_returns_the_schema_and_a_board_tool_is_not_offered():
    described = await run_agent_services(DESCRIBE_ACTION, {"name": SEARCH}, open_tools=_opener(_Remote()))
    assert described["service"]["input_schema"]["properties"] == {"query": {"type": "string"}}
    with pytest.raises(DomainError) as refused:
        await run_agent_services(DESCRIBE_ACTION, {"name": BOARD}, open_tools=_opener(_Remote()))
    assert refused.value.code == "agent_service_not_on_card"


@pytest.mark.asyncio
async def test_a_call_carries_its_invocation_id_and_returns_a_bounded_result():
    remote = _Remote()
    result = await run_agent_services(
        CALL_ACTION, {"name": SEARCH, "arguments": {"query": "x"}, "invocation_id": "inv-1"},
        open_tools=_opener(remote),
    )
    assert remote.calls[0]["name"] == SEARCH
    assert remote.calls[0]["arguments"] == {"query": "x"}
    assert remote.calls[0]["meta"] == {INVOCATION_META_KEY: "inv-1"}
    assert result["result"] == {"hits": 1} and result["truncated"] is False
    assert result["invocation_id"] == "inv-1"


@pytest.mark.asyncio
async def test_an_oversized_result_is_truncated_to_the_bound():
    big = {"structuredContent": {"text": "x" * (MAX_RESULT_BYTES * 2)}, "content": []}
    result = await run_agent_services(
        CALL_ACTION, {"name": SEARCH, "arguments": {"query": "x"}, "invocation_id": "inv-2"},
        open_tools=_opener(_Remote(big)),
    )
    assert result["truncated"] is True
    assert len(result["result"].encode("utf-8")) <= MAX_RESULT_BYTES
    assert result["result_bytes"] > MAX_RESULT_BYTES


@pytest.mark.asyncio
async def test_a_refusal_keeps_its_code_and_recovery_and_never_the_upstream_text():
    refused = {
        "isError": True,
        "content": [{"type": "text", "text": "upstream secret detail: sk-live-123"}],
        "structuredContent": {"error": {
            "code": "consent_required",
            "message": "upstream secret detail: sk-live-123",
            "recovery": {"type": "consent_required", "authorization_url": "https://hub/consent?x=1",
                         "invocation_id": "inv-3", "secret": "nope"},
        }},
    }
    with pytest.raises(DomainError) as raised:
        await run_agent_services(
            CALL_ACTION, {"name": SEARCH, "arguments": {"query": "x"}, "invocation_id": "inv-3"},
            open_tools=_opener(_Remote(refused)),
        )
    error = raised.value
    assert error.code == "consent_required"
    assert error.details["recovery"] == {"type": "consent_required", "authorization_url": "https://hub/consent?x=1",
                                         "invocation_id": "inv-3"}
    assert "sk-live" not in str(error) and "sk-live" not in str(error.details)


@pytest.mark.asyncio
@pytest.mark.parametrize("payload, code", [
    ({"name": SEARCH, "arguments": [1], "invocation_id": "i"}, "agent_service_arguments_invalid"),
    ({"name": SEARCH, "arguments": {}, "invocation_id": ""}, "agent_service_invocation_id_missing"),
    ({"name": "unknown", "arguments": {}, "invocation_id": "i"}, "agent_service_not_on_card"),
])
async def test_a_bad_call_is_refused_before_anything_is_sent(payload, code):
    remote = _Remote()
    with pytest.raises(DomainError) as raised:
        await run_agent_services(CALL_ACTION, payload, open_tools=_opener(remote))
    assert raised.value.code == code
    assert remote.calls == []


def test_brief_output_lists_services_and_bounds_a_result():
    listed = render_envelope({"ok": True, "result": {"operation": "agent_services.services", "services": [
        {"name": SEARCH, "title": "Search", "resource_kind": "remote_mcp", "arguments": ["query"]}]}})
    assert "services: 1" in listed and SEARCH in listed
    called = render_envelope({"ok": True, "result": {
        "operation": "agent_services.service-call", "name": SEARCH, "invocation_id": "inv-1",
        "result": "y" * 9000, "result_bytes": 9002, "truncated": False}})
    assert "invocation_id: inv-1" in called and "more characters" in called


def test_the_relay_sends_a_services_request_to_the_gateway_never_to_the_board(tmp_path):
    import asyncio

    from project_board.client import coordinate_queue
    from relay_helpers import StableClient, make_host, make_supervisor, submit_request
    from test_relay_coordinate_beside_cycle import _bound_session

    host, _identity, channel = make_host(tmp_path)
    queue = coordinate_queue.CoordinateQueue(host.field_root)
    asked = []

    class _ClientWithServices(StableClient):
        async def agent_services(self, action, payload):
            asked.append((action, dict(payload)))
            return {"services": [{"name": SEARCH}]}

    client = _ClientWithServices()
    supervisor = make_supervisor(host)
    supervisor._sessions[channel.worker_name] = _bound_session(host, channel, supervisor, client)

    async def scenario():
        request = submit_request(queue, channel, action=LIST_ACTION, object_ref="", payload={})
        assert supervisor.serve_coordinate_once() == [channel.worker_name]
        await asyncio.gather(*supervisor._coordinate_draining.values())
        return queue.take_response(worker_name=channel.worker_name, request_id=request["request_id"])

    response = asyncio.run(scenario())
    assert response is not None and response["ok"] is True, response
    assert response["result"] == {"services": [{"name": SEARCH}]}
    assert asked == [(LIST_ACTION, {})]
    assert client.calls == [], "a services request never reaches the board's Data Bus"


def test_a_relay_without_a_card_bearer_refuses_services_by_name(tmp_path):
    import asyncio

    from project_board.client import coordinate_queue
    from relay_helpers import StableClient, make_host, make_supervisor, submit_request
    from test_relay_coordinate_beside_cycle import _bound_session

    host, _identity, channel = make_host(tmp_path)
    queue = coordinate_queue.CoordinateQueue(host.field_root)
    client = StableClient()
    supervisor = make_supervisor(host)
    supervisor._sessions[channel.worker_name] = _bound_session(host, channel, supervisor, client)

    async def scenario():
        request = submit_request(queue, channel, action=LIST_ACTION, object_ref="", payload={})
        supervisor.serve_coordinate_once()
        await asyncio.gather(*supervisor._coordinate_draining.values())
        return queue.take_response(worker_name=channel.worker_name, request_id=request["request_id"])

    response = asyncio.run(scenario())
    assert response is not None and response["ok"] is False
    assert response["error"]["code"] == "work_agent_services_unavailable"
    assert client.calls == []
