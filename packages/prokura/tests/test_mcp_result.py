from __future__ import annotations

import json

import pytest

from prokura.mcp_result import bind_chat_result_handling


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
