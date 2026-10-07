"""Host source composition, with explicitly synthetic ports, not live acceptance."""
from __future__ import annotations

import importlib
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from connection_hub.delegated_credentials.cards.model import CardAuthority
from connection_hub.delegated_credentials.oauth_issuance import OAuthIssuancePlan
from test_host_composition import _load_entrypoint_module


@pytest.fixture
def host():
    entrypoint = _load_entrypoint_module()
    return importlib.import_module(entrypoint.__package__ + ".surfaces.oauth_original_exchange_host")


@pytest.mark.parametrize("issuer", [None, "", " https://hub.test/oauth", "https://hub.test/oauth ",
                                   "https://user@hub.test/oauth", "https://hub.test:bad/oauth",
                                   "//hub.test/oauth", "https://hub.test/oauth?x=1", "https://hub.test/oauth#x"])
def test_unconfigured_or_invalid_issuer_is_not_repaired_from_a_request(host, issuer):
    with pytest.raises(host.OriginalExchangeHostingUnavailable, match="issuer_not_configured"):
        host.configured_issuer(issuer)


@pytest.mark.parametrize("issuer", ["https://public.test/public/oauth", "http://localhost:3000/public/oauth/"])
def test_explicit_existing_public_or_local_issuer_is_preserved(host, issuer):
    assert host.configured_issuer(issuer) == issuer.rstrip("/")


def test_namespace_is_stable_and_scope_separated(host):
    first = host.custody_namespace("ab", "c")
    assert first == host.custody_namespace("ab", "c")
    assert first != host.custody_namespace("a", "bc")
    assert first != host.custody_namespace("ab", "d")


@pytest.mark.asyncio
async def test_full_consumed_candidate_choices_are_preserved_without_aliasing(host):
    payload = {"sub": "human", "client_id": "client", "card_label": "Reviewed label",
        "scopes": ["read"], "operations": ["search"], "resource_grants": {"service": ["read"]},
        "resource_operations": {"service": ["search"]}, "resource": "service", "registry_access_id": "card",
        "card_kind": "agent", "identity_scope": "project", "account_scope": {"p": {"a": ["claim"]}},
        "named_service_operations": {"mode": "none", "operations": {}}, "catalog_version": "old-basis",
        "client_metadata": {"client_name": "App", "client_metadata": {"worker": "unit"}}, "properties": {"reviewed": True},
        "expected_card_revision": 5, "invocation_policies": {}}
    actual = await host.consumed_candidate_inputs(payload=payload)
    assert actual == {"grantor_subject": "human", "client_id": "client", "client_label": "Reviewed label",
        **{key: value for key, value in payload.items() if key in {
            "scopes", "operations", "resource_grants", "resource_operations", "resource", "card_kind",
            "identity_scope", "account_scope", "named_service_operations", "catalog_version",
            "properties", "expected_card_revision", "invocation_policies"}}, "client_metadata": {"worker": "unit"},
        "access_id": "card", "replace_authority": True}
    actual["account_scope"]["p"]["a"].append("different")
    assert payload["account_scope"]["p"]["a"] == ["claim"]


@pytest.mark.asyncio
@pytest.mark.parametrize("choice", [None, {}, {"service": {"write": "once"}}, {"service": {"write": "always"}}])
async def test_consumed_invocation_choices_share_the_owning_original_arguments(host, choice):
    from kdcube_ai_app.apps.chat.sdk.integrations.connection_hub.delegated_credentials.oauth.original_candidate_inputs import oauth_issuance_arguments
    payload = {"sub": "human", "client_id": "client", "invocation_policies": choice}
    actual = await host.consumed_candidate_inputs(payload=payload)
    assert oauth_issuance_arguments(actual) == actual
    assert ("invocation_policies" in actual) == (choice is not None)
    if choice is not None:
        assert actual["invocation_policies"] == choice
        if choice:
            choice["service"]["write"] = "changed"
            assert actual["invocation_policies"]["service"]["write"] != "changed"


@pytest.mark.asyncio
async def test_host_calls_the_owning_label_helper_with_consumed_metadata(host, monkeypatch):
    from kdcube_ai_app.apps.chat.sdk.integrations.connection_hub.delegated_credentials.oauth import card_labels
    calls = []
    def label(metadata, *, resource, explicit):
        calls.append((metadata, resource, explicit))
        return "Owning label"
    monkeypatch.setattr(card_labels, "oauth_card_label", label)
    metadata = {"client_name": "Registered product", "client_metadata": {"worker": "unit"}}
    actual = await host.consumed_candidate_inputs(payload={"sub": "human", "client_id": "client",
        "client_metadata": metadata, "resource": "/mcp/records", "card_label": "Chosen label"})
    assert calls == [(metadata, "/mcp/records", "Chosen label")]
    assert actual["client_label"] == "Owning label" and actual["client_metadata"] == {"worker": "unit"}


@pytest.mark.asyncio
async def test_absent_policy_selection_is_not_changed_to_empty(host):
    actual = await host.consumed_candidate_inputs(payload={"sub": "human", "client_id": "client"})
    assert "invocation_policies" not in actual


def _scene(host, *, create=False):
    candidate = CardAuthority(access_id="oauth-card", client_id="client", grantor_subject="human",
        delegate_subject="integration:human:client", source="oauth", card_kind="agent", label="Reviewed",
        card_revision=1 if create else 2, expires_at=2000, operations=("search",),
        resource_grants={"service": ("read",)}, resource_operations={"service": ("search",)})
    original = None if create else replace(candidate, card_revision=1, label="Before")
    plan = OAuthIssuancePlan(transaction_id="a" * 64, decision_request_id="b" * 64, intent_digest="c" * 64,
        original_input_digest="d" * 64, tenant="tenant", project="project", access_id=candidate.access_id,
        grantor_subject="human", client_id="client", credential_issuer="delegated_client",
        credential_subject=candidate.delegate_subject, base_revision=0 if create else 1,
        candidate_revision=candidate.card_revision, expires_at=2000, card_content_hash=candidate.content_hash(),
        operations=candidate.operations, resource_grants=candidate.resource_grants,
        resource_operations=candidate.resource_operations, delivery_deadline=1700, reserved_until=1500,
        slots=("access", "refresh"), effect_digests={"access": "e" * 64, "refresh": "f" * 64})
    row = {"transaction_id": plan.transaction_id, "plan": {**plan.to_dict(), "intent": {
        "candidate": candidate.to_dict(), "original": None if original is None else original.to_dict()}}}
    store = SimpleNamespace(tenant="tenant", project="project", read_issuance_plan=AsyncMock(return_value=row),
                            issuance_clock=AsyncMock(return_value=1000))
    hub = SimpleNamespace(read_oauth_issuance_plan=AsyncMock(return_value=plan))
    cards = SimpleNamespace(read_current_authority=AsyncMock(return_value=None if create else (object(), original)))
    target = host.OriginalTarget(tenant="tenant", project="project", hub=hub, issuance_store=store, cards=cards)
    return SimpleNamespace(candidate=candidate, original=original, plan=plan, row=row, store=store,
                           hub=hub, cards=cards, target=target)


@pytest.mark.asyncio
@pytest.mark.parametrize("create", [False, True])
async def test_pending_fence_checks_exact_original_or_authoritative_absence(host, create):
    scene = _scene(host, create=create)
    assert await scene.target.fence(plan=scene.plan, result=SimpleNamespace(state="pending")) is True
    scene.cards.read_current_authority.return_value = (object(), scene.candidate)
    assert await scene.target.fence(plan=scene.plan, result=SimpleNamespace(state="pending")) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["label", "revision", "owner", "client", "kind", "state", "missing"])
async def test_committed_fence_requires_the_complete_exact_candidate(host, change):
    scene = _scene(host)
    scene.cards.read_current_authority.return_value = (object(), scene.candidate)
    committed = SimpleNamespace(state="committed")
    assert await scene.target.fence(plan=scene.plan, result=committed) is True
    field, value = {"label": ("label", "Changed"), "revision": ("card_revision", 3),
        "owner": ("grantor_subject", "other"), "client": ("client_id", "other"),
        "kind": ("card_kind", "connector"), "state": ("state", "revoked"),
        "missing": ("state", "revoked")}[change]
    scene.cards.read_current_authority.return_value = (None if change == "missing"
                                                      else (object(), replace(scene.candidate, **{field: value})))
    assert await scene.target.fence(plan=scene.plan, result=committed) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["namespace", "hub_plan", "row_missing", "row_transaction", "candidate",
                                  "original", "row_public_plan", "clock", "corrupt_pointer"])
async def test_host_fences_refuse_changed_or_unavailable_original_evidence(host, case):
    scene = _scene(host)
    if case == "namespace":
        scene.store.project = "other"
    elif case == "hub_plan":
        scene.hub.read_oauth_issuance_plan.return_value = replace(scene.plan, candidate_revision=3)
    elif case == "row_missing":
        scene.store.read_issuance_plan.return_value = None
    elif case == "row_transaction":
        scene.row["transaction_id"] = "0" * 64
    elif case == "candidate":
        scene.row["plan"]["intent"]["candidate"]["card_kind"] = "connector"
    elif case == "original":
        scene.row["plan"]["intent"]["original"]["grantor_subject"] = "other"
    elif case == "row_public_plan":
        scene.row["plan"]["delivery_deadline"] += 1
    elif case == "clock":
        scene.store.issuance_clock.return_value = scene.plan.reserved_until
        assert await scene.target.fence(plan=scene.plan, result=SimpleNamespace(state="pending")) is False
        return
    else:
        scene.cards.read_current_authority.side_effect = RuntimeError("pointer unavailable")
    with pytest.raises((host.OriginalExchangeHostingUnavailable, RuntimeError)):
        await scene.target.fence(plan=scene.plan, result=SimpleNamespace(state="pending"))


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["issuer", "pool", "signer", "session", "custody", "none"])
async def test_real_sdk_flow_is_bound_only_after_host_qualification(host, monkeypatch, failure):
    from kdcube_ai_app.auth import session_authority_runtime
    from kdcube_ai_app.infra.secrets import issuance
    from kdcube_ai_app.apps.chat.sdk.integrations.connection_hub.delegated_credentials.oauth.http.original_code import OriginalCodeExchangeHandler
    request = SimpleNamespace(state=SimpleNamespace())
    scope = host.custody_namespace("tenant", "project")
    custody = SimpleNamespace(namespace=scope, qualify=AsyncMock())
    if failure == "custody":
        custody.qualify.side_effect = RuntimeError("not qualified")
    monkeypatch.setattr(issuance, "issuance_secret_custody", lambda **kwargs: custody)
    monkeypatch.setattr(session_authority_runtime, "bundle_session_store_for",
                        lambda **kwargs: None if failure == "session" else object())
    scene = _scene(host)
    kwargs = dict(tenant="tenant", project="project", pg_pool=None if failure == "pool" else object(),
        configured_public_issuer=None if failure == "issuer" else "https://configured.test/public/oauth",
        hub=scene.hub, grant_store=SimpleNamespace(refresh_ttl=3600), issuance_store=scene.store,
        cards=scene.cards, settings=object(), refresh_signing_secret_ref="" if failure == "signer" else "protected.ref",
        resolve_secret=AsyncMock(return_value=b"test-only-signing-key-more-than-32-bytes"))
    if failure != "none":
        with pytest.raises((host.OriginalExchangeHostingUnavailable, RuntimeError)):
            await host.bind_original_exchange(request, **kwargs)
        assert request.state.oauth_original_exchange_factory is None
        return
    await host.bind_original_exchange(request, **kwargs)
    bound = request.state.oauth_original_exchange_factory()
    assert type(bound) is OriginalCodeExchangeHandler
    assert (bound.tenant, bound.project) == ("tenant", "project")
    assert request.state.oauth_delegated_issuer == kwargs["configured_public_issuer"]
    flow = bound.exchange.__self__
    assert flow.hub is scene.hub and flow.grant_store is kwargs["grant_store"]
    assert flow.ledger._pool is kwargs["pg_pool"]
    assert flow.provider.refresh_store._db._pool is kwargs["pg_pool"]
    assert flow.provider.authority_factory.__name__ == "get_bundle_session_authority"
    kwargs["resolve_secret"].assert_not_awaited()
    assert custody.qualify.await_count == 1


@pytest.mark.asyncio
async def test_metadata_preparation_is_a_separate_explicit_host_boundary(host, monkeypatch):
    from kdcube_ai_app.apps.chat.sdk.integrations.connection_hub.delegated_credentials.oauth import original_exchange_store, original_refresh_store
    calls = []

    class Metadata:
        def __init__(self, **kwargs):
            calls.append(kwargs)

        async def ensure_schema(self):
            calls.append("prepare")

    monkeypatch.setattr(original_exchange_store, "PostgresOriginalExchangeStore", Metadata)
    monkeypatch.setattr(original_refresh_store, "PostgresOriginalRefreshStore", Metadata)
    pool = object()
    await host.prepare_original_exchange_metadata(pg_pool=pool, tenant="tenant", project="project")
    assert calls == [{"pg_pool": pool, "tenant": "tenant", "project": "project"}, "prepare"] * 2


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["bound", "missing_issuer", "missing_redis", "service_unavailable"])
async def test_real_entrypoint_selects_only_its_host_binding(host, monkeypatch, case):
    entrypoint = _load_entrypoint_module()
    cfg = {"tenant": "tenant", "project": "project", "enabled": True,
           "issuer": "https://configured.test/public/oauth"}
    connections = {"card_transactions": {"enabled": True}, "delegated_credentials": {"oauth": {
        "enabled": True, "issuer": None if case == "missing_issuer" else cfg["issuer"],
        "original_exchange": {"refresh_signing_secret_ref": "protected.ref"}}}}
    grant_store = SimpleNamespace(refresh_ttl=3600)
    hub, cards, issuance_store = object(), object(), object()
    request = SimpleNamespace(state=SimpleNamespace(oauth_original_exchange_factory="browser-supplied"))
    owner = SimpleNamespace(redis=None if case == "missing_redis" else object(), pg_pool=object(), bundle_props={})
    monkeypatch.setattr(entrypoint, "_oauth_adapter_config", lambda *args: cfg)
    monkeypatch.setattr(entrypoint, "_connections_config", lambda *args: connections)
    monkeypatch.setattr(entrypoint, "_delegated_authority_config",
                        lambda *args: SimpleNamespace(backend="postgresql", generation_id="generation", uses_postgresql=True))
    monkeypatch.setattr(entrypoint, "_authority_registry_config", lambda *args: {})
    monkeypatch.setattr(entrypoint, "_oauth_grant_store", AsyncMock(return_value=grant_store))
    monkeypatch.setattr(entrypoint, "oauth_delegated_config", lambda *args: SimpleNamespace(tenant="tenant", project="project"))
    service = AsyncMock(return_value=hub)
    if case == "service_unavailable":
        service.side_effect = RuntimeError("service unavailable")
    monkeypatch.setattr(entrypoint, "_automation_access_service_for", service)
    monkeypatch.setattr(entrypoint, "_delegated_card_persistence", AsyncMock(return_value=SimpleNamespace(card_store=cards)))
    monkeypatch.setattr(entrypoint, "_durable_authority", lambda *args: SimpleNamespace(oauth=issuance_store))
    bound_calls = []

    async def bind(req, **kwargs):
        # Even a successful service factory cannot inherit a browser-selected
        # exchange capability: the actual entrypoint closed it first.
        assert req.state.oauth_original_exchange_factory is None
        host.configured_issuer(kwargs["configured_public_issuer"])
        bound_calls.append(kwargs)
        req.state.oauth_original_exchange_factory = lambda: "host-owned"

    monkeypatch.setattr(host, "bind_original_exchange", bind)
    if case == "service_unavailable":
        with pytest.raises(RuntimeError, match="service unavailable"):
            await entrypoint._bind_delegated_client_request_config(owner, request)
    else:
        await entrypoint._bind_delegated_client_request_config(owner, request)
    if case != "bound":
        assert request.state.oauth_original_exchange_factory is None
        assert bound_calls == []
        return
    assert request.state.oauth_original_exchange_factory() == "host-owned"
    actual, = bound_calls
    assert actual["pg_pool"] is owner.pg_pool
    assert actual["hub"] is hub and actual["cards"] is cards
    assert actual["grant_store"] is grant_store and actual["issuance_store"] is issuance_store
    assert actual["refresh_signing_secret_ref"] == "protected.ref"
