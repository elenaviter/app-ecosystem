from __future__ import annotations

import json

import pytest

from connection_hub.mcp_result import bind_chat_result_handling


def tool_for(result: object) -> object:
    class Tool:
        name = "named_services_search"

        async def run(self) -> object:
            return result

    tool = Tool()
    tool.coroutine = tool.run
    return tool


@pytest.mark.asyncio
async def test_agent_denial_uses_the_self_describing_consent_block() -> None:
    denial = json.dumps(
        {
            "ok": False,
            "error": "delegated_consent_required",
            "consent": {
                "agent_client_id": "kdcube-agent:app@v1:main",
                "resource": "*/app@v1/public/mcp/services*",
                "claims": ["mail:read"],
                "tool_name": "mail",
                "namespace": "mail",
                "operation": "object.search",
            },
        }
    )
    announced: list[object] = []

    async def announce(consent: object) -> None:
        announced.append(consent)

    tool = tool_for((denial, {"artifact": None}))
    bind_chat_result_handling([tool], agent_consent_announcer=announce)
    content, artifact = await tool.coroutine()

    assert artifact == {"artifact": None}
    assert len(announced) == 1
    result = json.loads(content)
    grant = result["consent"]["grant"]["payload"]
    assert grant["named_service_operations"] == {"mail": ["object.search"]}


@pytest.mark.asyncio
async def test_outer_operation_card_denial_becomes_an_exact_consent_demand() -> None:
    resource = "https://service.example/mcp"
    denial = json.dumps(
        {
            "ok": False,
            "error": {
                "code": "delegated_capability_not_granted",
                "message": "This delegated card does not grant that operation.",
            },
            "ret": {
                "requested_capability": {
                    "kind": "outer_operation",
                    "resource": resource,
                    "outer_operation": "records_export",
                }
            },
            "consent": {
                "agent_client_id": "kdcube-agent:app@v1:main",
                "resource": resource,
                "claims": [],
                "tool_name": "records_export",
                "outer_operation": "records_export",
            },
        }
    )
    announced: list[object] = []

    async def announce(consent: object) -> None:
        announced.append(consent)

    tool = tool_for(denial)
    bind_chat_result_handling([tool], agent_consent_announcer=announce)
    result = json.loads(await tool.coroutine())

    assert len(announced) == 1
    assert result["consent"]["grant"]["payload"]["resource_operations"] == {
        resource: ["records_export"]
    }


@pytest.mark.asyncio
async def test_consumed_once_policy_opens_the_existing_card_without_regranting() -> None:
    resource = "urn:connection-hub:remote-mcp:mcp_0123456789abcdef01234567"
    denial = json.dumps(
        {
            "ok": False,
            "error": "remote_mcp_proxy_denied",
            "code": "delegated_invocation_limit_exhausted",
            "consent": {
                "kind": "delegated_invocation_policy",
                "agent_client_id": "kdcube-agent:app@v1:main",
                "access_id": "access-1",
                "resource": resource,
                "outer_operation": "search",
                "tool_name": "search",
                "connection_hub_url": "https://hub.example/card/access-1",
                "available_choices": ["allow_once", "allow_always"],
            },
        }
    )
    announced: list[object] = []

    async def announce(consent: object) -> None:
        announced.append(consent)

    tool = tool_for(denial)
    bind_chat_result_handling([tool], agent_consent_announcer=announce)
    result = json.loads(await tool.coroutine())

    assert len(announced) == 1
    assert result["error"]["code"] == "delegated_invocation_limit_exhausted"
    assert result["consent"]["connection_hub_url"] == (
        "https://hub.example/card/access-1"
    )
    assert "grant" not in result["consent"]


@pytest.mark.asyncio
async def test_ungranted_remote_tool_preserves_exact_policy_grant_demand() -> None:
    resource = "urn:connection-hub:remote-mcp:mcp_0123456789abcdef01234567"
    grant = {
        "operation": "delegated_agent_grant_create",
        "payload": {
            "client_id": "claude-code",
            "access_id": "access-1",
            "resource": resource,
            "claims": ["external_mcp:use"],
            "resource_operations": {resource: ["delete"]},
            "invocation_change_id": "invoke-delete-1",
        },
    }
    denial = json.dumps(
        {
            "ok": False,
            "error": "remote_mcp_proxy_denied",
            "code": "operation_not_consented",
            "consent": {
                "kind": "delegated_agent_grant",
                "agent_client_id": "claude-code",
                "access_id": "access-1",
                "resource": resource,
                "claims": ["external_mcp:use"],
                "outer_operation": "delete",
                "tool_name": "external_ops_delete",
                "connection_hub_url": "https://hub.example/grant",
                "invocation_policy": "choose",
                "invocation_change_id": "invoke-delete-1",
                "available_choices": ["allow_once", "allow_always"],
                "grant": grant,
            },
        }
    )
    announced: list[object] = []

    async def announce(consent: object) -> None:
        announced.append(consent)

    tool = tool_for(denial)
    bind_chat_result_handling([tool], agent_consent_announcer=announce)
    result = json.loads(await tool.coroutine())

    assert len(announced) == 1
    consent = announced[0]
    assert consent.consent["access_id"] == "access-1"
    assert consent.consent["invocation_policy"] == "choose"
    assert consent.consent["invocation_change_id"] == "invoke-delete-1"
    assert consent.consent["grant"] == grant
    event = consent.chat_event_payload()["consent"]
    assert event["outer_operation"] == "delete"
    assert event["available_choices"] == ["allow_once", "allow_always"]
    assert event["invocation_policy"] == "choose"
    assert event["invocation_change_id"] == "invoke-delete-1"
    assert result["error"]["code"] == "operation_not_consented"
    assert result["consent"]["grant"] == grant


@pytest.mark.asyncio
async def test_direct_admission_grant_preserves_exact_policy_transaction() -> None:
    resource = "https://service.example/customers"
    grant = {
        "operation": "delegated_agent_grant_create",
        "payload": {
            "client_id": "external-client",
            "access_id": "access-1",
            "resource": resource,
            "claims": [],
            "resource_operations": {resource: ["customers.delete"]},
            "invocation_change_id": "invoke-delete-1",
        },
    }
    denial = json.dumps(
        {
            "ok": False,
            "error": {
                "code": "delegated_capability_not_granted",
                "message": "This delegated card does not grant that operation.",
            },
            "consent": {
                "kind": "delegated_agent_grant",
                "agent_client_id": "external-client",
                "access_id": "access-1",
                "resource": resource,
                "claims": [],
                "tool_name": "customers.delete",
                "outer_operation": "customers.delete",
                "connection_hub_url": "https://hub.example/grant",
                "invocation_policy": "choose",
                "invocation_change_id": "invoke-delete-1",
                "available_choices": ["allow_once", "allow_always"],
                "grant": grant,
            },
        }
    )
    announced: list[object] = []

    async def announce(consent: object) -> None:
        announced.append(consent)

    tool = tool_for(denial)
    bind_chat_result_handling([tool], agent_consent_announcer=announce)
    result = json.loads(await tool.coroutine())

    assert len(announced) == 1
    consent = announced[0]
    assert consent.consent["access_id"] == "access-1"
    assert consent.consent["invocation_change_id"] == "invoke-delete-1"
    assert consent.consent["grant"] == grant
    event = consent.chat_event_payload()["consent"]
    assert event["outer_operation"] == "customers.delete"
    assert event["available_choices"] == ["allow_once", "allow_always"]
    assert event["invocation_policy"] == "choose"
    assert event["invocation_change_id"] == "invoke-delete-1"
    assert result["error"]["code"] == "delegated_capability_not_granted"
    assert result["consent"]["grant"] == grant


@pytest.mark.asyncio
async def test_connected_account_denial_uses_the_connected_consent_port() -> None:
    payload = {
        "ok": False,
        "error": {
            "code": "needs_connected_account_consent",
            "details": {
                "consent": {
                    "provider_id": "google",
                    "claims": ["gmail:read"],
                    "namespace": "mail",
                }
            },
        },
    }
    announced: list[dict] = []

    async def announce(value: dict, *, namespace: str, tool_name: str) -> None:
        announced.append(
            {"value": value, "namespace": namespace, "tool_name": tool_name}
        )

    tool = tool_for(json.dumps(payload))
    bind_chat_result_handling([tool], connected_consent_announcer=announce)
    result = await tool.coroutine()

    assert json.loads(result) == payload
    assert announced[0]["namespace"] == "mail"
    assert announced[0]["tool_name"] == "mail"


@pytest.mark.asyncio
async def test_file_delivery_port_replaces_only_the_model_visible_result() -> None:
    payload = {
        "ok": True,
        "object": {
            "object_ref": "mail:account:file:1",
            "download": {"encoding": "url", "url": "https://example.invalid/signed"},
        },
    }

    async def deliver(value: dict) -> dict:
        if value != payload:
            return value
        return {
            "ok": True,
            "delivery": "The file was delivered to the user.",
        }

    tool = tool_for({"content": [{"type": "text", "text": json.dumps(payload)}]})
    bind_chat_result_handling([tool], file_deliverer=deliver)
    result = await tool.coroutine()

    visible = json.loads(result["content"][0]["text"])
    assert visible["delivery"] == "The file was delivered to the user."
    assert "signed" not in result["content"][0]["text"]
