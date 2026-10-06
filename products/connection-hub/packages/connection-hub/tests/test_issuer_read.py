"""Author read-contract tests; not mounted authentication or PB nonce proof."""
import asyncio
import copy
import dataclasses
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from connection_hub.delegated_credentials.automation_access import AutomationAccessService
from connection_hub.delegated_credentials.cards.persistence import DurableCardPersistence
from connection_hub.delegated_credentials.cards.service import DelegatedCardService
from connection_hub.delegated_credentials.cards.store import BundleStorageDelegatedCardStore
from connection_hub.delegated_credentials.issuer_read import (
    IssuerReadDecision, IssuerReadQuery, IssuerReadRegistry, IssuerReadRequest, IssuerReadRefused,
    RemoteIssuerReadAdapter, issuer_read_request_from_mapping, read_digest, project_identity,
    sign_issuer_read_request, verify_issuer_read_request,
)
from connection_hub.delegated_credentials.remote_issuer import verify_issuer_request, sign_issuer_request
from test_issuer_gate import REQUEST as WRITE_REQUEST
from test_lifecycle_store import _pair, _seed, _wire, ACTOR, MOMENT


def query():
    return IssuerReadQuery.from_mapping({"context_ref": '{"purpose":"opaque-é"}', "request_id": "read-1",
        "targets": [{"owner_subject": c.grantor_subject, "access_id": c.access_id,
                     "issuer_kind": c.issuer_kind, "issuer_ref": c.issuer_ref} for c in _pair()]})


def request():
    q = query()
    return IssuerReadRequest(ACTOR, "registered", "tenant", "hosting-project", q.context_ref, q.request_id, q.targets)


def signed(req=None, **kwargs):
    return sign_issuer_read_request(secret="x" * 32, bundle_id="opaque@1-0", operation="read-identity",
        service_id="hub", request=req or request(), now=1000, **kwargs)


def verify(body, **kwargs):
    args = dict(secret="x" * 32, bundle_id="opaque@1-0", operation="read-identity",
                expected_service_id="hub", body=body, now=1000)
    return verify_issuer_read_request(**{**args, **kwargs})


def test_strict_public_query_sorted_pair_and_complete_utf8_request_binding():
    q = query()
    assert IssuerReadQuery.from_mapping({**q.to_dict(), "targets": q.to_dict()["targets"][::-1]}) == q
    req = request()
    assert issuer_read_request_from_mapping(req.to_dict()) == req
    assert req.read_digest == read_digest({"protocol": "issuer-read.v1", "request": req.payload()})
    for raw in ({**q.to_dict(), "actor_subject": ACTOR}, {**q.to_dict(), "targets": q.to_dict()["targets"][:1]},
                {**q.to_dict(), "targets": [q.to_dict()["targets"][0]] * 2},
                {**q.to_dict(), "context_ref": '{ "purpose": "opaque-é" }'}):
        with pytest.raises(IssuerReadRefused):
            IssuerReadQuery.from_mapping(raw)
    for key, value in (("actor_subject", "other"), ("tenant", "other"), ("project", "other"),
                       ("actor_classification", "external"), ("read_digest", "0" * 64)):
        with pytest.raises(IssuerReadRefused):
            issuer_read_request_from_mapping({**req.to_dict(), key: value})


@pytest.mark.parametrize("changed", ["bundle_id", "operation", "expected_service_id", "secret", "expiry"])
def test_read_proof_bound_to_recipient_operation_service_secret_and_expiry(changed):
    body = signed()
    assert verify(body).allowed
    changes = {changed: "other"} if changed != "expiry" else {"now": 100000}
    assert not verify(body, **changes).allowed


def test_read_write_proofs_are_not_interchangeable_and_each_call_has_fresh_nonce():
    body = signed()
    assert body["service_proof"]["nonce"] != signed()["service_proof"]["nonce"]
    assert not verify_issuer_request(secret="x" * 32, bundle_id="opaque@1-0", operation="read-identity",
        expected_service_id="hub", body=body, now=1000).allowed
    write = sign_issuer_request(secret="x" * 32, bundle_id="opaque@1-0", operation="read-identity",
        service_id="hub", request=WRITE_REQUEST, now=1000)
    assert not verify(write).allowed
    for field in ("request", "phase", "snapshots", "service_proof"):
        forged = copy.deepcopy(body)
        forged[field] = {} if field != "phase" else "validate"
        assert not verify(forged).allowed
    # Authentication is NOT consumption: PB's durable shared nonce claim is
    # separately required. No misleading process-local replay cache here.
    assert verify(body).allowed and verify(body).allowed


def test_identity_projector_never_copies_unknown_maps_or_secret_leaves_and_uses_full_hash():
    card = dataclasses.replace(_pair()[0], client_id="credential-owner",
        properties={"identity": {"subject": "opaque", "unknown": "hidden", "token": "do-not-export"}},
        provenance={"unapproved": "hidden"})
    view = project_identity(card, (("properties", "identity", "subject"),))
    assert view["properties"] == {"identity": {"subject": "opaque"}}
    assert "client_id" not in view and "provenance" not in view
    changed = dataclasses.replace(card, client_id="changed-hidden-field")
    assert project_identity(changed, (("properties", "identity", "subject"),)) == view
    assert changed.content_hash() != card.content_hash()
    for path in (("properties", "identity"), ("properties", "identity", "token")):
        if path[-1] == "token":
            with pytest.raises(IssuerReadRefused):
                project_identity(card, (path,))
        else:
            assert "properties" not in project_identity(card, (path,))


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["", "first-denied", "second-denied", "policy-moved", "expired",
                                      "wrong-echo", "wrong-snapshot", "partial", "missing-adapter"])
async def test_pair_authorized_before_storage_and_revalidated_only_after_both_fences_release(tmp_path, failure):
    cards = _pair()
    store = BundleStorageDelegatedCardStore(tmp_path, lifecycle_lock_scope="same-host-flock")
    await _seed(store, cards)
    before_files = {p: p.read_bytes() for p in store.root.rglob("*") if p.is_file()}
    held, acquired, calls = [], [], []
    clock = [datetime.now(timezone.utc)]

    @asynccontextmanager
    async def locks(**kwargs):
        held.append(kwargs["resource_id"])
        acquired.append(kwargs["lock_path"])
        try:
            yield {}
        finally:
            held.pop()

    async def transport(payload):
        assert held == [], "peer I/O ran while a Card fence was held"
        assert payload["request"] == request().to_dict()
        calls.append(payload)
        validate = payload["phase"] == "validate"
        assert len(payload["snapshots"]) == (2 if validate else 0)
        if failure == "expired" and validate:
            clock[0] += timedelta(seconds=31)
        response = {"ok": True, "request": payload["request"], "phase": payload["phase"],
            "snapshots_digest": read_digest(payload["snapshots"]), "decision": {
                "allowed": not (failure == "first-denied" or failure == "second-denied" and validate),
                "reason": "opaque-policy-refused", "policy_version": "moved" if failure == "policy-moved" and validate else "v1",
                "valid_until": (clock[0] + timedelta(seconds=30)).isoformat()}}
        if failure == "wrong-echo":
            response["request"] = {**payload["request"], "actor_subject": "forged"}
        if failure == "wrong-snapshot" and validate:
            response["snapshots_digest"] = "0" * 64
        if failure == "partial" and validate:
            response.pop("phase")
        return response

    registry = IssuerReadRegistry(now=lambda: clock[0])
    for card in cards:
        if failure != "missing-adapter":
            registry.register(RemoteIssuerReadAdapter(issuer_kind=card.issuer_kind, adapter_id=card.issuer_kind, transport=transport))
    cache, handles = MagicMock(), MagicMock()
    persistence = DurableCardPersistence(redis=object(), tenant="fixture", project="fixture", card_store=store,
                                         credential_handles=handles, mutation_lock=locks)
    persistence._cards._cache = cache
    handles.reset_mock()  # constructor's presence check is not read I/O
    port = AutomationAccessService.__new__(AutomationAccessService)
    port._persistence = persistence
    port.bind_issuer_read_registry(registry, actor_subject=ACTOR, actor_classification="registered", tenant="tenant", project="hosting-project")
    result = await port.issuer_managed_lifecycle_read(query().to_dict())
    assert held == []
    assert cache.mock_calls == [] and handles.mock_calls == []
    assert {p: p.read_bytes() for p in store.root.rglob("*") if p.is_file()} == before_files
    if failure:
        assert result["ok"] is False and "snapshots" not in result
        if failure in ("first-denied", "wrong-echo", "missing-adapter"):
            assert acquired == []
    else:
        assert result["ok"] and len(calls) == 4 and len(result["snapshots"]) == 2
        assert acquired == sorted(acquired, key=lambda p: (p.parent.parent.parent.name, p.parent.name))
        for row, card in zip(result["snapshots"], cards):
            assert row["authority_fingerprint"] == card.content_hash()
            assert "client_id" not in row["identity"]


@pytest.mark.asyncio
async def test_prepared_intent_refuses_retryably_without_recovery_or_card_cache_handle_writes(tmp_path):
    from connection_hub.delegated_credentials.cards import lifecycle_store
    from connection_hub.delegated_credentials.cards.lifecycle import LifecycleRequest
    cards = _pair()
    store = BundleStorageDelegatedCardStore(tmp_path, lifecycle_lock_scope="same-host-flock")
    await _seed(store, cards)
    lifecycle = LifecycleRequest.from_mapping(_wire(cards))
    async def stop():
        raise asyncio.CancelledError("fixture-kill-after-preparation")
    async def allow():
        return datetime.now(timezone.utc) + timedelta(seconds=30)
    with pytest.raises(asyncio.CancelledError):
        await lifecycle_store.atomic_revoke(store, request=lifecycle, actor_subject=ACTOR, now=MOMENT,
                                           before_publish=allow, after_prepare=stop)
    before = {p: p.read_bytes() for p in store.root.rglob("*") if p.is_file()}
    @asynccontextmanager
    async def lock(**kwargs):
        yield {}
    cache = MagicMock()
    service = DelegatedCardService(store=store, cache=cache, mutation_lock=lock)
    with pytest.raises(IssuerReadRefused) as exc:
        await service.read_lifecycle_identities(request())
    assert exc.value.reason == "issuer_read_lifecycle_pending" and exc.value.retryable
    assert {p: p.read_bytes() for p in store.root.rglob("*") if p.is_file()} == before
    assert cache.mock_calls == []


@pytest.mark.asyncio
async def test_read_seals_are_registry_local_phase_and_evidence_bound_and_not_write_seals():
    now = datetime.now(timezone.utc)
    class Adapter:
        issuer_kind = "opaque-0"
        adapter_id = "read-only"
        async def decide_read(self, request, **kwargs):
            return True, "", "v1", now + timedelta(seconds=30)
    registry = IssuerReadRegistry()
    registry.register(Adapter())
    req = request()
    real = await registry.decide(req, issuer_kind="opaque-0")
    registry.require(req, real, issuer_kind="opaque-0", phase="authorize")
    forged = dataclasses.replace(real, allowed=False)
    other = IssuerReadRegistry()
    other.register(Adapter())
    for decision, reg in ((forged, registry), (real, other), (WRITE_REQUEST, registry),
                           (dataclasses.replace(real, _seal=None), registry)):
        with pytest.raises(IssuerReadRefused):
            reg.require(req, decision, issuer_kind="opaque-0", phase="authorize")


@pytest.mark.asyncio
async def test_missing_explicit_lock_capability_and_owner_equal_external_classification_fail_closed(tmp_path):
    @asynccontextmanager
    async def lock(**kwargs):
        pytest.fail("unconfigured lock was acquired")
        yield {}
    service = DelegatedCardService(store=BundleStorageDelegatedCardStore(tmp_path), cache=MagicMock(), mutation_lock=lock)
    with pytest.raises(IssuerReadRefused, match="atomic_fences_unavailable"):
        await service.read_lifecycle_identities(request())
    external = dataclasses.replace(request(), actor_subject="owner-0", actor_classification="external")
    with pytest.raises(IssuerReadRefused, match="requires_platform_human"):
        issuer_read_request_from_mapping(external.to_dict())
