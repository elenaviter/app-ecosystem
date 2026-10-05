from __future__ import annotations

import ast
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from connection_hub.delegated_credentials.issuer_gate import (
    IssuerDecision, IssuerRegistry, IssuerRequest, change_digest, issuer_write_refusal,
)

NOW = datetime(2026, 10, 5, 21, tzinfo=timezone.utc)
REQUEST = IssuerRequest("actor", "request", "update", "access", 3,
                        "external-authority", "opaque-scope", change_digest({"label": "new"}),
                        "trusted-context")
RECORD = SimpleNamespace(access_id="access", card_revision=3,
                         issuer_kind="external-authority", issuer_ref="opaque-scope")


class Adapter:
    issuer_kind = "external-authority"
    adapter_id = "configured-adapter"

    def __init__(self):
        self.calls = []
        self.allowed = True
        self.reason = ""
        self.version = "policy-1"
        self.until = NOW + timedelta(seconds=30)
        self.failure = False

    async def decide(self, request):
        self.calls.append(request)
        if self.failure:
            raise RuntimeError("sensitive transport details")
        return self.allowed, self.reason, self.version, self.until


def configured():
    registry = IssuerRegistry(now=lambda: NOW)
    adapter = Adapter()
    registry.register(adapter)
    return registry, adapter


@pytest.mark.asyncio
async def test_sealed_exact_receipt_and_two_fresh_non_consuming_reads():
    registry, adapter = configured()
    issued = await registry.decide(REQUEST)
    assert issuer_write_refusal(RECORD, REQUEST, issued, now=NOW) is None
    fresh = await registry.revalidate(REQUEST, issued)
    assert fresh.allowed
    assert adapter.calls == [REQUEST, REQUEST]
    assert issuer_write_refusal(RECORD, REQUEST, fresh, now=NOW) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("actor_subject", "another"), ("request_id", "another"),
    ("action", "revoke"), ("access_id", "another"),
    ("card_revision", 4), ("issuer_kind", "another"),
    ("issuer_ref", "another"), ("change_digest", change_digest({"label": "other"})),
    ("context_ref", "client-forged"),
])
async def test_every_request_dimension_is_exactly_bound(field, value):
    registry, _ = configured()
    issued = await registry.decide(REQUEST)
    crossed = replace(REQUEST, **{field: value})
    assert issuer_write_refusal(RECORD, crossed, issued, now=NOW)["status"] == 403
    assert not (await registry.revalidate(crossed, issued)).allowed


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("request", replace(REQUEST, actor_subject="other")), ("allowed", False),
    ("reason", "forged"), ("policy_version", "other"),
    ("valid_until", NOW + timedelta(seconds=59)), ("adapter_id", "other"),
])
async def test_dataclass_replace_cannot_reuse_a_seal_for_changed_authority(field, value):
    registry, _ = configured()
    issued = await registry.decide(REQUEST)
    forged = replace(issued, **{field: value})
    assert issuer_write_refusal(RECORD, REQUEST, forged, now=NOW)["reason"] == "issuer_decision_unsealed"
    assert not (await registry.revalidate(REQUEST, forged)).allowed


@pytest.mark.asyncio
async def test_dict_json_and_manually_constructed_receipts_have_no_authority():
    registry, _ = configured()
    issued = await registry.decide(REQUEST)
    for forged in (asdict(issued), {}, None, IssuerDecision(
            REQUEST, True, "", "policy-1", NOW + timedelta(seconds=30), "configured-adapter")):
        assert issuer_write_refusal(RECORD, REQUEST, forged, now=NOW)["reason"] == "issuer_decision_unsealed"
        assert not (await registry.revalidate(REQUEST, forged)).allowed


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("access_id", "other"), ("card_revision", 4),
    ("issuer_kind", "other"), ("issuer_ref", "other"),
])
async def test_actual_target_record_binding_is_checked(field, value):
    registry, _ = configured()
    issued = await registry.decide(REQUEST)
    crossed = SimpleNamespace(**{**vars(RECORD), field: value})
    assert issuer_write_refusal(crossed, REQUEST, issued, now=NOW)["reason"] == "issuer_card_binding_mismatch"


@pytest.mark.asyncio
async def test_expiry_boundary_naive_and_too_long_windows_refuse():
    registry, adapter = configured()
    issued = await registry.decide(REQUEST)
    assert issuer_write_refusal(RECORD, REQUEST, issued, now=issued.valid_until)["reason"] == "issuer_decision_expired"
    for until, reason in [(NOW, "issuer_decision_expired"),
                          (NOW + timedelta(seconds=61), "issuer_decision_window_exceeded"),
                          (NOW.replace(tzinfo=None), "issuer_response_invalid")]:
        adapter.until = until
        assert (await registry.decide(REQUEST)).reason == reason


@pytest.mark.asyncio
async def test_missing_unknown_and_raising_adapters_fail_closed():
    registry = IssuerRegistry(now=lambda: NOW, owner_managed_kinds=("ordinary",))
    assert not registry.is_managed("ordinary")
    assert registry.is_managed("unknown")
    assert not (await registry.decide(REQUEST)).allowed
    registry, adapter = configured()
    adapter.failure = True
    assert (await registry.decide(REQUEST)).reason == "issuer_adapter_unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["denied", "version", "unavailable", "replacement"])
async def test_revalidation_sees_current_authority_and_adapter(mutation):
    registry, adapter = configured()
    issued = await registry.decide(REQUEST)
    if mutation == "denied":
        adapter.allowed, adapter.reason = False, "required_card_revoked"
    elif mutation == "version":
        adapter.version = "policy-2"
    elif mutation == "unavailable":
        adapter.failure = True
    else:
        registry.register(Adapter())
    assert not (await registry.revalidate(REQUEST, issued)).allowed


@pytest.mark.asyncio
async def test_identically_named_adapter_in_another_registry_is_not_authority():
    registry, _ = configured()
    other, _ = configured()
    issued = await other.decide(REQUEST)
    assert not (await registry.revalidate(REQUEST, issued)).allowed


@pytest.mark.asyncio
async def test_revalidation_never_extends_initial_window():
    registry, adapter = configured()
    issued = await registry.decide(REQUEST)
    adapter.until = NOW + timedelta(seconds=60)
    fresh = await registry.revalidate(REQUEST, issued)
    assert fresh.valid_until == issued.valid_until


@pytest.mark.parametrize("invalid_request", [replace(REQUEST, card_revision=True),
                                     replace(REQUEST, context_ref=""),
                                     replace(REQUEST, action="read")])
@pytest.mark.asyncio
async def test_invalid_trusted_request_fails_before_adapter(invalid_request):
    registry, adapter = configured()
    assert not (await registry.decide(invalid_request)).allowed
    assert adapter.calls == []


def test_digest_canonicalization_and_non_json_values():
    assert change_digest({"b": [1], "a": "ü"}) == change_digest({"a": "ü", "b": (1,)})
    assert change_digest({"label": "old"}) != change_digest({"label": "new"})
    with pytest.raises(ValueError):
        change_digest({"value": float("nan")})


def test_new_gate_has_no_domain_imports_or_domain_literals():
    import connection_hub.delegated_credentials.issuer_gate as gate
    tree = ast.parse(Path(gate.__file__).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert not any(part in (node.module or "") for part in ("project", "board"))
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert "work:project:" not in node.value
            assert "project.cards.manage" not in node.value
