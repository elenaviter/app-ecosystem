"""A consent review's extras commit with the agent's request in one Card write, or not at all."""
from __future__ import annotations

from pathlib import Path

import pytest

from kdcube_ai_app.apps.chat.sdk.runtime.dynamic_module_loader import load_dynamic_module_for_path

CLIENT = "kdcube-agent:problem-board@1-0:worker"
NAMED = "https://hub.example.test/mcp/named-services"
MEMORIES = "https://hub.example.test/mcp/memories"
SERVICE = "https://hub.example.test/mcp/records"


class _Cards:
    def __init__(self, refuse: str = ""):
        self.creates, self.refuse = [], refuse

    async def list_access(self, user):
        return {"ok": True, "platform_user_id": "user-1", "items": [], "resources": []}

    async def create_access(self, user, **kwargs):
        self.creates.append(kwargs)
        if self.refuse:
            return {"ok": False, "error": self.refuse, "details": {"outside": [{"resource": MEMORIES,
                                                                               "operation": "memories:read"}]}}
        return {"ok": True, "access_id": "agent-1", "access": {"access_id": "agent-1", "card_revision": 8}}


def _world(monkeypatch, cards):
    _name, module = load_dynamic_module_for_path(Path(__file__).resolve().parents[1] / "entrypoint.py")
    from kdcube_ai_app.apps.chat.sdk.integrations.connection_hub.delegated_to_kdcube import consent_demand

    async def author(**_kwargs):
        return 1

    async def access_service(*_args, **_kwargs):
        return cards

    monkeypatch.setattr(consent_demand, "author_consent_granted_events", author)
    monkeypatch.setattr(module, "_automation_access_service", access_service)
    monkeypatch.setattr(module, "_platform_user_payload", lambda *a, **kw: {"sub": "user-1", "user_id": "user-1"})
    instance = module.ConnectionHubEntrypoint.__new__(module.ConnectionHubEntrypoint)
    instance.redis = object()

    async def grant(**extra):
        return await module.ConnectionHubEntrypoint.delegated_agent_grant_create(instance, data={
            "client_id": CLIENT, "resource": NAMED, "claims": ["named_services:use"],
            "named_service_operations": {"slides": ["object.list"]}, "expected_card_revision": 7, **extra})

    return grant


@pytest.mark.asyncio
async def test_the_request_and_its_extras_are_one_card_write(monkeypatch):
    cards = _Cards()
    grant = _world(monkeypatch, cards)
    answer = await grant(extras=[
        {"resource": MEMORIES, "claims": ["memories:read"]},
        {"resource": SERVICE, "claims": ["records:read"], "resource_operations": {SERVICE: ["records_export"]}},
        {"resource": NAMED, "claims": [], "named_service_operations": {"sheets": ["values.update"]}},
    ])
    assert answer["ok"] is True, answer
    [write] = cards.creates
    assert write["resource_grants"] == {NAMED: ["named_services:use"], MEMORIES: ["memories:read"],
                                        SERVICE: ["records:read"]}
    assert write["resource_operations"] == {SERVICE: ["records_export"]}
    assert write["named_service_operations"] == {NAMED: {"slides": ["object.list"], "sheets": ["values.update"]}}
    assert write["merge_existing"] is True and write["expected_card_revision"] == 7


@pytest.mark.asyncio
async def test_a_refused_extra_refuses_the_whole_write(monkeypatch):
    cards = _Cards(refuse="agent_capability_selection_outside_control")
    grant = _world(monkeypatch, cards)
    answer = await grant(extras=[{"resource": MEMORIES, "claims": ["memories:read"]}])
    assert answer["ok"] is False and answer["error"] == "agent_capability_selection_outside_control"
    assert len(cards.creates) == 1, "one write: nothing of the request was committed separately"


@pytest.mark.asyncio
async def test_extras_never_join_a_replacing_edit(monkeypatch):
    cards = _Cards()
    grant = _world(monkeypatch, cards)
    answer = await grant(replace=True, extras=[{"resource": MEMORIES, "claims": ["memories:read"]}])
    assert answer["ok"] is False and answer["error"] == "invalid_delegated_access_request"
    assert cards.creates == []
