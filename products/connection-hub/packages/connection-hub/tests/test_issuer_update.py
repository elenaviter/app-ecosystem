"""Author contract/issuer tests; not mounted policy or cross-process replay proof."""
import dataclasses
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from connection_hub.delegated_credentials.issuer_gate import IssuerRegistry, IssuerWriteRefused
from connection_hub.delegated_credentials.issuer_update import (
    IssuerUpdateQuery, IssuerUpdateRefused, build_candidate, issuer_managed_card_update,
)
from test_lifecycle_store import _pair


ACTOR = "actual-human"


def card():
    return dataclasses.replace(_pair()[0], label="Préservé", issuer_label="External issuer", manage_url="https://issuer.example/cards",
        resource_operations={"opaque-resource": ("read",), "unchanged-resource": ("other",)},
        resource_grants={"opaque-resource": ("read",), "unchanged-resource": ("other-grant",)},
        named_services={"service": {"identity": "keep"}}, account_scope={"provider": {"account": ("scope",)}},
        provenance={"original": {"preserve": True}}, created_at=100, last_issued_at=200,
        properties={"opaque-property": {"original": "value"}})


def wire(original=None):
    original = original or card()
    return {"context_ref": "opaque-immutable-intent", "request_id": "fixture-one-card-update",
        "target": {"owner_subject": original.grantor_subject, "access_id": original.access_id,
            "issuer_kind": original.issuer_kind, "issuer_ref": original.issuer_ref,
            "expected_card_revision": original.card_revision, "expected_authority_fingerprint": original.content_hash()},
        "delta": {"resource": "opaque-resource", "operations": ["read", "write"], "grants": ["read", "write"]}}


@pytest.mark.parametrize("field", ["actor_subject", "tenant", "project", "candidate", "change_digest", "approval", "decision", "secret"])
def test_query_cannot_supply_authority_or_a_candidate(field):
    with pytest.raises(IssuerUpdateRefused):
        IssuerUpdateQuery.from_mapping({**wire(), field: "forged"})


@pytest.mark.parametrize("selection", [["write", "read"], ["read", "read"], [" read"], [False], "read"])
def test_selections_are_strict_not_normalized_claims(selection):
    raw = wire()
    raw["delta"]["operations"] = selection
    with pytest.raises(IssuerUpdateRefused):
        IssuerUpdateQuery.from_mapping(raw)


def test_complete_original_is_preserved_except_exact_selection_revision_and_derived_union():
    original = card()
    candidate = build_candidate(original, IssuerUpdateQuery.from_mapping(wire(original)))
    assert candidate.card_revision == original.card_revision + 1
    assert candidate.resource_operations == {**original.resource_operations, "opaque-resource": ("read", "write")}
    assert candidate.resource_grants == {**original.resource_grants, "opaque-resource": ("read", "write")}
    allowed = {"card_revision", "operations", "resource_operations", "resource_grants"}
    assert {k: v for k, v in candidate.to_dict().items() if k not in allowed} == {
        k: v for k, v in original.to_dict().items() if k not in allowed}
    assert original == card()


@pytest.mark.parametrize("field", ["operations", "grants"])
def test_narrowing_is_not_supported(field):
    raw = wire()
    raw["delta"][field] = []
    with pytest.raises(IssuerUpdateRefused, match="narrowing_unsupported"):
        build_candidate(card(), IssuerUpdateQuery.from_mapping(raw))


@pytest.mark.parametrize("field,value", [("expected_card_revision", 2), ("expected_authority_fingerprint", "0" * 64),
    ("issuer_kind", "other"), ("owner_subject", "other")])
def test_every_original_target_binding_is_exact(field, value):
    raw = wire()
    raw["target"][field] = value
    with pytest.raises(IssuerUpdateRefused):
        build_candidate(card(), IssuerUpdateQuery.from_mapping(raw))


def registry(*, allow=True, until_seconds=30, finalize=True):
    result = IssuerRegistry()
    calls = []

    class Adapter:
        issuer_kind = card().issuer_kind
        adapter_id = "fixture-issuer"

        async def decide(self, request):
            calls.append(request)
            assert request.actor_subject == ACTOR
            return allow, "" if allow else "fixture_policy_denied", "v1", datetime.now(timezone.utc) + timedelta(seconds=until_seconds)

        async def finalize_context(self, request, *, outcome):
            return finalize

    result.register(Adapter())
    return result, calls


@pytest.mark.asyncio
async def test_missing_registry_port_or_actual_host_never_calls_persistence():
    persistence = MagicMock()
    persistence.update_issuer = AsyncMock()
    for reg, host in ((None, lambda: True), (IssuerRegistry(), None), (IssuerRegistry(), lambda: False)):
        result = await issuer_managed_card_update(wire(), actor_subject=ACTOR, registry=reg,
                                                  persistence=persistence, host_is_current=host)
        assert result["ok"] is False and "authority" not in result
    persistence.update_issuer.assert_not_called()


@pytest.mark.asyncio
async def test_actual_publication_deadline_cannot_outlive_the_original_card():
    expires = datetime.now(timezone.utc) + timedelta(seconds=5)
    # Credentialless Controls correctly forbid expiry. Use a valid bounded
    # credential-bearing authority, with no live credential material here.
    original = dataclasses.replace(card(), source="oauth", expires_at=expires.timestamp())
    raw = wire(original)
    query = IssuerUpdateQuery.from_mapping(raw)
    candidate = build_candidate(original, query)
    reg, calls = registry(until_seconds=30)
    persistence = MagicMock()
    persistence.read_issuer_update_authority = AsyncMock()

    async def inspect_deadline(query, *, actor_subject, before_commit):
        assert await before_commit(original, candidate) == expires
        assert await before_commit(original, candidate) == expires
        raise IssuerWriteRefused("fixture_no_publish")

    persistence.update_issuer = AsyncMock(side_effect=inspect_deadline)
    result = await issuer_managed_card_update(raw, actor_subject=ACTOR, registry=reg,
        persistence=persistence, host_is_current=lambda: True)
    assert result["error"] == "fixture_no_publish" and len(calls) == 2
    persistence.read_issuer_update_authority.assert_not_called()
