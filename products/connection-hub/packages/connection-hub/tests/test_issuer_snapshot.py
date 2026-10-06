"""Author full-snapshot contract tests; not mounted authentication or PB nonce proof.

The full snapshot is a second, separately authorized read beside the identity
read: its own protocol, proof, decision and seal. It returns both complete
original Card authorities or neither, decided before the fenced storage read
and again after it, with no peer I/O, cache, handle or file write inside.
"""
import dataclasses
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from connection_hub.delegated_credentials.cards.persistence import DurableCardPersistence
from connection_hub.delegated_credentials.cards.store import BundleStorageDelegatedCardStore
from connection_hub.delegated_credentials.issuer_read import (
    IssuerReadQuery, IssuerReadRegistry, IssuerReadRequest, RemoteIssuerReadAdapter, read_digest,
    sign_issuer_read_request, verify_issuer_read_request,
)
from connection_hub.delegated_credentials.issuer_snapshot import (
    IssuerSnapshotDecision, IssuerSnapshotRefused, IssuerSnapshotRegistry, IssuerSnapshotRequest,
    RemoteIssuerSnapshotAdapter, full_authority, issuer_managed_card_snapshots,
    issuer_snapshot_request_from_mapping, sign_issuer_snapshot_request, verify_issuer_snapshot_request,
)
from test_lifecycle_store import _pair, _seed, ACTOR

HOST = dict(actor_subject=ACTOR, actor_classification="registered", tenant="tenant", project="hosting-project")


def query():
    return IssuerReadQuery.from_mapping({"context_ref": '{"purpose":"migration"}', "request_id": "snap-1",
        "targets": [{"owner_subject": c.grantor_subject, "access_id": c.access_id,
                     "issuer_kind": c.issuer_kind, "issuer_ref": c.issuer_ref} for c in _pair()]})


def request():
    q = query()
    return IssuerSnapshotRequest(ACTOR, "registered", "tenant", "hosting-project", q.context_ref, q.request_id, q.targets)


def signed(req=None, **kwargs):
    return sign_issuer_snapshot_request(secret="x" * 32, bundle_id="opaque@1-0", operation="full-snapshot",
        service_id="hub", request=req or request(), now=1000, **kwargs)


def verify(body, **kwargs):
    args = dict(secret="x" * 32, bundle_id="opaque@1-0", operation="full-snapshot",
                expected_service_id="hub", body=body, now=1000)
    return verify_issuer_snapshot_request(**{**args, **kwargs})


# ── the request and its proof ───────────────────────────────────────────────


def test_strict_request_binding_and_a_snapshot_digest_of_its_own():
    req = request()
    assert issuer_snapshot_request_from_mapping(req.to_dict()) == req
    assert req.snapshot_digest == read_digest({"protocol": "issuer-snapshot.v1", "request": req.payload()})
    identity = IssuerReadRequest(*dataclasses.astuple(req)[:6], req.targets)
    assert req.snapshot_digest != identity.read_digest
    for key, value in (("actor_subject", "other"), ("tenant", "other"), ("project", "other"),
                       ("actor_classification", "external"), ("snapshot_digest", "0" * 64)):
        with pytest.raises(IssuerSnapshotRefused):
            issuer_snapshot_request_from_mapping({**req.to_dict(), key: value})
    for subject in ("anonymous", "integration:bot", "telegram_1"):
        with pytest.raises(IssuerSnapshotRefused, match="platform_human"):
            issuer_snapshot_request_from_mapping({**req.to_dict(), "actor_subject": subject})


@pytest.mark.parametrize("changed", ["bundle_id", "operation", "expected_service_id", "secret", "expiry"])
def test_snapshot_proof_bound_to_recipient_operation_service_secret_and_expiry(changed):
    body = signed()
    assert verify(body).allowed
    changes = {changed: "other"} if changed != "expiry" else {"now": 100000}
    assert not verify(body, **changes).allowed


def test_snapshot_and_identity_read_proofs_are_not_interchangeable():
    snapshot = signed()
    assert not verify_issuer_read_request(secret="x" * 32, bundle_id="opaque@1-0", operation="full-snapshot",
        expected_service_id="hub", body=snapshot, now=1000).allowed
    q = query()
    identity = sign_issuer_read_request(secret="x" * 32, bundle_id="opaque@1-0", operation="full-snapshot",
        service_id="hub", request=IssuerReadRequest(ACTOR, "registered", "tenant", "hosting-project",
                                                    q.context_ref, q.request_id, q.targets), now=1000)
    assert not verify(identity).allowed
    assert snapshot["service_proof"]["nonce"] != signed()["service_proof"]["nonce"]


# ── the payload: complete, or refused ───────────────────────────────────────


def test_full_authority_is_the_complete_original_payload():
    card = _pair()[0]
    assert full_authority(card) == card.to_dict()


SYNTHETIC_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJmaXh0dXJlIn0.c2lnbmF0dXJlLW9ubHktZml4dHVyZQ"


@pytest.mark.parametrize("change", [
    pytest.param({"properties": {"api_token": "x"}}, id="credential-key-name"),
    pytest.param({"provenance": {"nested": {"refresh_token": "x"}}}, id="nested-credential-key"),
    pytest.param({"properties": {"client_secret": "x"}}, id="client-secret-key"),
    pytest.param({"properties": {"credential_handle": "x"}}, id="credential-handle-key"),
    pytest.param({"client_metadata": {"note": "Bearer abcdefgh12345678"}}, id="bearer-value"),
    pytest.param({"properties": {"note": SYNTHETIC_JWT}}, id="jwt-value"),
    pytest.param({"named_services": {"svc": {"note": "ghp_" + "a" * 36}}}, id="token-prefix-value"),
    pytest.param({"properties": {"note": "aB3dE5fG7hJ9kL1mN3pQ5rS7tV9wX1yZ"}}, id="high-entropy-value"),
    pytest.param({"last_four": "abcde"}, id="last-four-longer-than-four"),
    pytest.param({"manage_url": "http://hub.example/manage/1"}, id="manage-url-not-https"),
    pytest.param({"manage_url": "https://user:pw@hub.example/manage/1"}, id="manage-url-userinfo"),
    pytest.param({"manage_url": "https://hub.example/manage/1?code=x"}, id="manage-url-query"),
    pytest.param({"manage_url": "https://hub.example/manage/1#frag"}, id="manage-url-fragment"),
])
def test_each_credential_shape_refuses_instead_of_being_redacted(change):
    card = dataclasses.replace(_pair()[0], **change)
    with pytest.raises(IssuerSnapshotRefused) as raised:
        full_authority(card)
    # The refusal names a reason only: no field value reaches it.
    assert raised.value.reason == "issuer_snapshot_credential_material"
    assert str(raised.value) == "issuer_snapshot_credential_material"


GHP = "ghp_" + "A1b2C3d4" * 5


class _Raw:
    """A Card-shaped payload; full_authority only reads to_dict()."""

    def __init__(self, raw):
        self._raw = raw

    def to_dict(self):
        return self._raw


def _raw_with(**changes):
    raw = _pair()[0].to_dict()
    raw.update(changes)
    return _Raw(raw)


@pytest.mark.parametrize("payload", [
    pytest.param(_raw_with(account_scope={"svc": {"api_token": ["x"]}}), id="account-scope-key"),
    pytest.param(_raw_with(resource_acceptance={"urn:fixture": {"note": GHP}}), id="resource-acceptance-value"),
    pytest.param(_raw_with(label=GHP), id="label-value"),
    pytest.param(_raw_with(identity_scope=GHP), id="top-level-string-value"),
    pytest.param(_raw_with(operations=[GHP]), id="operation-list-value"),
    pytest.param(_raw_with(control_card={"control_id": GHP, "issuer_kind": "project", "issuer_ref": "p"}),
                 id="control-card-value"),
    pytest.param(_raw_with(access_id="AKIA" + "ABCDEFGH12345678"), id="identity-field-issued-secret-shape"),
])
def test_every_field_family_is_scanned_not_only_the_free_form_ones(payload):
    with pytest.raises(IssuerSnapshotRefused, match="credential_material"):
        full_authority(payload)


@pytest.mark.parametrize("value", [0, False, 1234, ["ab"]])
def test_a_non_string_last_four_refuses_even_when_falsy(value):
    with pytest.raises(IssuerSnapshotRefused, match="credential_material"):
        full_authority(_raw_with(last_four=value))


def test_identity_fields_may_look_random_without_refusing():
    # Opaque identifiers are exempt from the high-entropy rule only.
    opaque = "aB3dE5fG7hJ9kL1mN3pQ5rS7tV9wX1yZ"
    card = dataclasses.replace(_pair()[0], access_id=opaque, client_id=opaque)
    assert full_authority(card)["access_id"] == opaque
    with pytest.raises(IssuerSnapshotRefused):
        full_authority(dataclasses.replace(_pair()[0], label=opaque))


def test_no_false_refusal_on_a_card_shaped_like_the_live_inventory():
    # Synthetic values in the live shapes: resource names that contain the
    # word "credentials", URNs, lowercase hex digests, timestamps, a safe
    # manage URL and a four-character last_four.
    resource = "*/api/integrations/bundles/*/*/problem-board@1-0/public/mcp/problem_board*"
    card = dataclasses.replace(
        _pair()[0],
        last_four="ab12",
        manage_url="https://hub.example/connections/manage/card-1",
        properties={"project_name": "fixture project", "hub": "urn:connection-hub:remote-mcp:mcp_" + "0" * 24,
                    "storage": "connection-hub-1-0/delegated_credentials/cards/v1", "seats": 3, "enabled": True},
        provenance={"project_identity_edge": {"edge_hash": "a" * 64, "at": "2026-10-05T12:00:00Z",
                                              "resource": resource},
                    "project_person_control_audit": [{"op": "project.cards.manage", "by": "fixture-owner"}]},
        # Standard non-secret OAuth client metadata keys seen in the live store.
        client_metadata={"display": "Delegated credentials for fixture project",
                         "token_endpoint_auth_method": "none", "kdcube_credential_use": "delegated"},
    )
    assert full_authority(card) == card.to_dict()


# ── decisions are separate from the identity read ───────────────────────────


@pytest.mark.asyncio
async def test_snapshot_seals_are_registry_local_and_not_identity_read_decisions():
    until = datetime.now(timezone.utc) + timedelta(seconds=30)

    class Adapter:
        issuer_kind, adapter_id = _pair()[0].issuer_kind, "a"

        async def decide_snapshot(self, req, *, phase, snapshots):
            return True, "", "v1", until

        async def decide_read(self, req, *, phase, snapshots):
            return True, "", "v1", until

    req = request()
    registry = IssuerSnapshotRegistry()
    registry.register(Adapter())
    real = await registry.decide(req, issuer_kind=Adapter.issuer_kind)
    registry.require(req, real, issuer_kind=Adapter.issuer_kind, phase="authorize")
    other = IssuerSnapshotRegistry()
    other.register(Adapter())
    with pytest.raises(IssuerSnapshotRefused, match="unsealed"):
        other.require(req, real, issuer_kind=Adapter.issuer_kind, phase="authorize")
    with pytest.raises(IssuerSnapshotRefused, match="unsealed"):
        registry.require(req, dataclasses.replace(real, allowed=True, reason="forged"),
                         issuer_kind=Adapter.issuer_kind, phase="authorize")
    identity_registry = IssuerReadRegistry()
    identity_registry.register(Adapter())
    q = query()
    identity_decision = await identity_registry.decide(
        IssuerReadRequest(ACTOR, "registered", "tenant", "hosting-project", q.context_ref, q.request_id, q.targets),
        issuer_kind=Adapter.issuer_kind)
    with pytest.raises(IssuerSnapshotRefused, match="unsealed"):
        registry.require(req, identity_decision, issuer_kind=Adapter.issuer_kind, phase="authorize")


def test_an_adapter_without_a_snapshot_decision_cannot_register():
    adapter = RemoteIssuerReadAdapter(issuer_kind="project", adapter_id="read-only", transport=lambda p: p)
    with pytest.raises(ValueError, match="issuer_snapshot_adapter_invalid"):
        IssuerSnapshotRegistry().register(adapter)


# ── the whole read, on the real fenced store ────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["", "first-denied", "second-denied", "policy-moved", "expired",
                                     "wrong-echo", "wrong-snapshot", "missing-adapter", "credential"])
async def test_both_full_snapshots_or_neither_decided_before_and_after_the_fences(tmp_path, failure, caplog):
    cards = _pair()
    if failure == "credential":
        cards = (dataclasses.replace(cards[0], properties={"secret": "x"}), cards[1])
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
        if validate:
            # The second decision sees the complete original payloads.
            assert [row["authority"] for row in payload["snapshots"]] == [c.to_dict() for c in cards]
        if failure == "expired" and validate:
            clock[0] += timedelta(seconds=31)
        response = {"ok": True, "request": payload["request"], "phase": payload["phase"],
            "snapshots_digest": read_digest(payload["snapshots"]), "decision": {
                "allowed": not (failure == "first-denied" or failure == "second-denied" and validate),
                "reason": "opaque-policy-refused",
                "policy_version": "moved" if failure == "policy-moved" and validate else "v1",
                "valid_until": (clock[0] + timedelta(seconds=30)).isoformat()}}
        if failure == "wrong-echo":
            response["request"] = {**payload["request"], "actor_subject": "forged"}
        if failure == "wrong-snapshot" and validate:
            response["snapshots_digest"] = "0" * 64
        return response

    registry = IssuerSnapshotRegistry(now=lambda: clock[0])
    for card in cards:
        if failure != "missing-adapter":
            registry.register(RemoteIssuerSnapshotAdapter(issuer_kind=card.issuer_kind, adapter_id=card.issuer_kind,
                                                          transport=transport))
    cache, handles = MagicMock(), MagicMock()
    persistence = DurableCardPersistence(redis=object(), tenant="fixture", project="fixture", card_store=store,
                                         credential_handles=handles, mutation_lock=locks)
    persistence._cards._cache = cache
    handles.reset_mock()  # constructor's presence check is not read I/O
    caplog.set_level(logging.DEBUG)
    result = await issuer_managed_card_snapshots(query().to_dict(), registry=registry, persistence=persistence, **HOST)

    assert held == []
    assert cache.mock_calls == [] and handles.mock_calls == []
    assert {p: p.read_bytes() for p in store.root.rglob("*") if p.is_file()} == before_files
    # No personal or Card payload reaches a log line or a refusal (Ops' condition).
    for card in cards:
        assert card.grantor_subject not in caplog.text and card.access_id not in caplog.text
    if failure:
        assert result["ok"] is False and set(result) == {"ok", "status", "error", "retryable"}
        assert not any(card.access_id in json.dumps(result) for card in cards)
        if failure in ("first-denied", "wrong-echo", "missing-adapter"):
            assert acquired == []
        if failure == "credential":
            assert result["error"] == "issuer_snapshot_credential_material"
    else:
        assert result["ok"] and len(calls) == 4 and len(result["snapshots"]) == 2
        assert result["snapshot_digest"] == request().snapshot_digest
        for row, card in zip(result["snapshots"], sorted(cards, key=lambda c: (c.grantor_subject, c.access_id))):
            assert row["authority"] == card.to_dict()
            assert row["authority_fingerprint"] == card.content_hash()
            assert row["card_revision"] == card.card_revision


@pytest.mark.asyncio
async def test_the_actor_and_scope_come_from_the_host_never_the_query(tmp_path):
    raw = {**query().to_dict(), "actor_subject": "forged"}
    result = await issuer_managed_card_snapshots(raw, registry=IssuerSnapshotRegistry(), persistence=MagicMock(), **HOST)
    assert result["ok"] is False and result["error"] == "issuer_snapshot_query_invalid"


@pytest.mark.asyncio
async def test_no_registry_or_no_storage_port_refuses():
    no_registry = await issuer_managed_card_snapshots(query().to_dict(), registry=None, persistence=MagicMock(), **HOST)
    assert no_registry["error"] == "issuer_snapshot_host_unavailable"

    class NoPort:
        pass

    no_port = await issuer_managed_card_snapshots(query().to_dict(), registry=IssuerSnapshotRegistry(),
                                                  persistence=NoPort(), **HOST)
    assert no_port["error"] == "issuer_snapshot_port_unavailable"


@pytest.mark.asyncio
async def test_a_non_human_host_actor_refuses_before_any_read():
    persistence = MagicMock()
    result = await issuer_managed_card_snapshots(query().to_dict(), registry=IssuerSnapshotRegistry(),
                                                 persistence=persistence,
                                                 **{**HOST, "actor_subject": "integration:bot"})
    assert result["error"] == "issuer_snapshot_requires_platform_human"
    assert persistence.read_lifecycle_identities.mock_calls == []
