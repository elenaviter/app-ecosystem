"""W749: an earlier, stuck PREPARED invocation-policy change never blocks a new attempt.

Live 11 Oct 17:46Z: the operator's My Card retry was refused invocation_policy_change_in_progress
by a PREPARED marker left at 13:46Z by an attempt whose Card write was refused. Operator, verbatim:
"i do not want anything that was prepared and stuck to get in my way". The direct path
(delegated_agent_grant_create) now releases its own marker when the Card write is refused or raises,
and a new change id supersedes any other PREPARED marker. The policy service and its file store are
real; the Card write is a recording stand-in.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from connection_hub.invocation_policy import (
    POLICY_CHANGE_COMMITTED,
    POLICY_CHANGE_PREPARED,
    POLICY_ONCE,
    SURFACE_OUTER,
    BundleStorageInvocationPolicyStore,
    InvocationAuthority,
    InvocationPolicyService,
    InvocationPolicyConflict,
)
from kdcube_ai_app.apps.chat.sdk.runtime.dynamic_module_loader import load_dynamic_module_for_path

RESOURCE = "https://reference.example.test/customers"
ACCESS_ID = "access_demo"
CLIENT_ID = "claude-code"
AUTHORITY = InvocationAuthority(access_id=ACCESS_ID, resource=RESOURCE, surface=SURFACE_OUTER, operation="restart")
REFUSED = {"ok": False, "error": "card_transactions_direct_write_refused", "status": 403}


def _entrypoint_module():
    _name, module = load_dynamic_module_for_path(Path(__file__).resolve().parents[1] / "entrypoint.py")
    return module


class _Cards:
    """The existing client's Card; each extend answers the next scripted outcome ("ok", "refuse", "raise")."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.card = {"access_id": ACCESS_ID, "client_id": CLIENT_ID,
                     "resource_grants": {RESOURCE: ["external_mcp:use"]},
                     "resource_operations": {RESOURCE: ["status"]},
                     "catalog_row_by_resource": {RESOURCE: f"{RESOURCE}*"}, "card_revision": 3}

    async def list_access(self, user):
        return {"ok": True, "platform_user_id": "user-1", "items": [dict(self.card)],
                "resources": [{"resource": f"{RESOURCE}*", "operations": [{"name": "status"}, {"name": "restart"}]}]}

    async def extend_client_access(self, user, **kwargs):
        outcome = self.outcomes.pop(0)
        if outcome == "raise":
            raise RuntimeError("simulated Card write failure")
        if outcome == "refuse":
            return dict(REFUSED)
        self.card["resource_operations"] = {RESOURCE: ["restart", "status"]}
        self.card["card_revision"] += 1
        return {"ok": True, "access_id": ACCESS_ID, "access": dict(self.card)}


def _world(monkeypatch, tmp_path, outcomes):
    module = _entrypoint_module()

    @asynccontextmanager
    async def lock(**_kwargs):
        yield {}

    policies = InvocationPolicyService(store=BundleStorageInvocationPolicyStore(tmp_path), mutation_lock=lock)
    cards = _Cards(outcomes)
    from kdcube_ai_app.apps.chat.sdk.integrations.connection_hub.delegated_to_kdcube import consent_demand

    async def author(**_kwargs):
        return 1

    async def access_service(*_args, **_kwargs):
        return cards

    monkeypatch.setattr(consent_demand, "author_consent_granted_events", author)
    monkeypatch.setattr(module, "_automation_access_service", access_service)
    monkeypatch.setattr(module, "_invocation_policy_service", lambda *a, **kw: policies)
    monkeypatch.setattr(module, "_platform_user_payload", lambda *a, **kw: {"sub": "user-1", "user_id": "user-1"})
    instance = module.ConnectionHubEntrypoint.__new__(module.ConnectionHubEntrypoint)
    instance.redis = object()

    async def save(change_id):
        return await module.ConnectionHubEntrypoint.delegated_agent_grant_create(instance, data={
            "client_id": CLIENT_ID, "access_id": ACCESS_ID, "resource": RESOURCE, "claims": [],
            "resource_operations": {RESOURCE: ["restart"]}, "invocation_mode": POLICY_ONCE,
            "invocation_change_id": change_id})

    async def marker():
        return await policies.get_policy_change(owner_subject="user-1", authority=AUTHORITY)

    return policies, cards, save, marker


@pytest.mark.asyncio
async def test_a_refused_card_write_releases_its_prepared_marker(monkeypatch, tmp_path):
    _policies, _cards, save, marker = _world(monkeypatch, tmp_path, ["refuse"])
    result = await save("edit-attempt-1")
    assert result == REFUSED
    assert await marker() is None  # nothing left PREPARED to block the next attempt


@pytest.mark.asyncio
async def test_a_card_write_that_raises_releases_its_prepared_marker(monkeypatch, tmp_path):
    _policies, _cards, save, marker = _world(monkeypatch, tmp_path, ["raise"])
    with pytest.raises(RuntimeError):
        await save("edit-attempt-1")
    assert await marker() is None


@pytest.mark.asyncio
async def test_card_transaction_policy_service_binds_fail_closed_recovery(monkeypatch, tmp_path):
    module = _entrypoint_module()

    @asynccontextmanager
    async def lock(**_kwargs):
        yield {}

    policies = InvocationPolicyService(store=BundleStorageInvocationPolicyStore(tmp_path), mutation_lock=lock)
    monkeypatch.setattr(module, "build_invocation_policy_service", lambda **_kwargs: policies)
    monkeypatch.setattr(module, "_storage_root_or_error", lambda _entrypoint: tmp_path)
    monkeypatch.setattr(module, "_connections_config", lambda _entrypoint: {"card_transactions": {"enabled": True}})
    monkeypatch.setattr(module, "_delegated_authority_config", lambda _entrypoint: type("Config", (), {
        "uses_postgresql": False,
    })())
    assert module._invocation_policy_service(object()) is policies
    assert policies._transaction_settler is not None
    change = await policies.prepare_policy_change(owner_subject="user-1", authority=AUTHORITY,
                                                  mode=POLICY_ONCE, change_id="edit-attempt-1")
    with pytest.raises(InvocationPolicyConflict, match="invocation_policy_earlier_change_undecided") as raised:
        await policies._transaction_settler(change, "user-1")
    assert module._invocation_policy_failure(raised.value)["retryable"] is True


@pytest.mark.asyncio
async def test_the_live_sequence_a_stuck_marker_never_blocks_a_new_attempt(monkeypatch, tmp_path):
    """Live: a pre-fix attempt left PREPARED (13:46Z); the retry carries a new id (17:46Z) and must commit."""
    policies, cards, save, marker = _world(monkeypatch, tmp_path, ["ok"])
    stuck = await policies.prepare_policy_change(owner_subject="user-1", authority=AUTHORITY, mode=POLICY_ONCE,
                                                 change_id="edit-pre-fix-attempt", now=100)
    assert stuck.state == POLICY_CHANGE_PREPARED
    result = await save("edit-retry-new-nonce")
    assert result["ok"] is True, result
    assert result["invocation_policy"]["mode"] == POLICY_ONCE
    current = await marker()
    assert (current.change_id, current.state) == ("edit-retry-new-nonce", POLICY_CHANGE_COMMITTED)
    assert cards.card["card_revision"] == 4


@pytest.mark.asyncio
async def test_a_refused_attempt_then_a_new_attempt_commits(monkeypatch, tmp_path):
    _policies, _cards, save, marker = _world(monkeypatch, tmp_path, ["refuse", "ok"])
    assert (await save("edit-attempt-1"))["ok"] is False
    result = await save("edit-attempt-2")
    assert result["ok"] is True, result
    current = await marker()
    assert (current.change_id, current.state) == ("edit-attempt-2", POLICY_CHANGE_COMMITTED)
