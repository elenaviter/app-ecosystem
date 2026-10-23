"""W929: a focused grant adds a resource the Card does not hold yet, with its policy, when the catalog offers it.

Live 23 Oct: a secret set from the Connection Hub CLI was refused resource_not_consented, and the consent page
it opened for the concrete secret could not be approved. delegated_agent_grant_create refused any resource
missing from the Card with invocation_policy_resource_not_granted before the Card write that would add it.
A resource a catalog row offers (here one secret under the kdcube_secret selector) is now added by the same
focused grant, so the grant and its Once/Always policy still commit together. The policy service and its file
store are real; the Card write is a recording stand-in, as in test_w749.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from connection_hub.invocation_policy import (
    POLICY_ONCE,
    SURFACE_OUTER,
    BundleStorageInvocationPolicyStore,
    InvocationAuthority,
    InvocationPolicyService,
)
from kdcube_ai_app.apps.chat.sdk.runtime.dynamic_module_loader import load_dynamic_module_for_path

DEPLOYMENT = "urn:kdcube:management:deployment:demo-tenant:demo-project"
SECRET_SELECTOR = "urn:kdcube:management:secret:*:*:*:*:*"
SECRET = "urn:kdcube:management:secret:demo-tenant:demo-project:bundle:connection-hub@1-0:connections.signing_key"
WRITE = "kdcube.management.secret.value.write"
ACCESS_ID = "oauth-cli"
CLIENT_ID = "dcr-cli"


def _entrypoint_module():
    _name, module = load_dynamic_module_for_path(Path(__file__).resolve().parents[1] / "entrypoint.py")
    return module


class _Cards:
    """A CLI Card that holds only the deployment resource; the catalog offers `rows`."""

    def __init__(self, rows):
        self.rows = rows
        self.extends = []
        self.card = {"access_id": ACCESS_ID, "client_id": CLIENT_ID,
                     "resource_grants": {"urn:kdcube:management:deployment:*:*": ["kdcube.management.inspect"]},
                     "resource_operations": {"urn:kdcube:management:deployment:*:*": ["kdcube.management.deployment.inspect"]},
                     "catalog_row_by_resource": {}, "card_revision": 1}

    async def list_access(self, user):
        return {"ok": True, "platform_user_id": "user-1", "items": [dict(self.card)], "resources": self.rows}

    async def extend_client_access(self, user, **kwargs):
        self.extends.append(kwargs)
        self.card["resource_grants"][kwargs["resource"]] = list(kwargs["claims"])
        self.card["resource_operations"][kwargs["resource"]] = [WRITE]
        self.card["card_revision"] += 1
        return {"ok": True, "access_id": ACCESS_ID, "access": dict(self.card)}


def _world(monkeypatch, tmp_path, rows):
    module = _entrypoint_module()

    @asynccontextmanager
    async def lock(**_kwargs):
        yield {}

    policies = InvocationPolicyService(store=BundleStorageInvocationPolicyStore(tmp_path), mutation_lock=lock)
    cards = _Cards(rows)
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

    async def save():
        return await module.ConnectionHubEntrypoint.delegated_agent_grant_create(instance, data={
            "client_id": CLIENT_ID, "access_id": ACCESS_ID, "resource": SECRET,
            "claims": ["kdcube.management.secret.write"],
            "resource_operations": {SECRET: [WRITE]}, "invocation_mode": POLICY_ONCE,
            "invocation_change_id": "consent-1"})

    return policies, cards, save


def _secret_row(resource=SECRET_SELECTOR, operations=(WRITE,)):
    return {"resource": resource, "selector_type": "kdcube_secret",
            "operations": [{"name": name} for name in operations]}


@pytest.mark.asyncio
async def test_a_secret_the_card_lacks_is_granted_with_its_policy_when_the_catalog_offers_it(monkeypatch, tmp_path, caplog):
    policies, cards, save = _world(monkeypatch, tmp_path, [_secret_row()])

    with caplog.at_level("INFO"):
        result = await save()

    assert result["ok"] is True, result
    assert result["invocation_policy"]["mode"] == POLICY_ONCE
    assert [call["resource"] for call in cards.extends] == [SECRET]  # the concrete secret, never the selector
    policy = await policies.get(owner_subject="user-1", authority=InvocationAuthority(
        access_id=ACCESS_ID, resource=SECRET, surface=SURFACE_OUTER, operation=WRITE))
    assert policy is not None and policy.mode == POLICY_ONCE
    # W929: the committed mode is logged, with no values.
    committed = [record.getMessage() for record in caplog.records if "policy committed" in record.getMessage()]
    assert committed == [
        f"[delegated_agent_grant] policy committed access_id={ACCESS_ID} resource={SECRET} operation={WRITE} "
        f"mode=once revision={policy.revision} change_id=consent-1"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("rows", [
    [],
    [{"resource": "*", "operations": [{"name": WRITE}]}],
    [_secret_row(resource="urn:kdcube:management:secret:other-tenant:*:*:*:*")],
], ids=["no-row", "all-resource-selector", "another-tenant"])
async def test_a_resource_no_catalog_row_offers_is_still_refused(monkeypatch, tmp_path, rows):
    _policies, cards, save = _world(monkeypatch, tmp_path, rows)

    result = await save()

    assert result == {"ok": False, "error": "invocation_policy_resource_not_granted", "status": 409}
    assert cards.extends == []


@pytest.mark.asyncio
async def test_an_offered_resource_without_the_requested_operation_is_refused(monkeypatch, tmp_path):
    _policies, cards, save = _world(monkeypatch, tmp_path, [_secret_row(operations=("kdcube.management.secret.metadata.read",))])

    result = await save()

    assert result["ok"] is False
    assert result["error"] == "invocation_policy_operation_not_available"
    assert cards.extends == []
