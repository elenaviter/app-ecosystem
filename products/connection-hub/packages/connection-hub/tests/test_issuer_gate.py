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


def test_existing_core_domain_coupling_may_only_shrink_from_frozen_base():
    """Source-derived semantic sets from AE 0a70e21c, not a fungible count.

    Generic authority projection and project_legacy_operations are projections,
    not external issuer policy. The remaining seven domain modules and six
    distinct literals (including three existing docstrings) are legacy debt.
    This phase may remove them, but cannot substitute a new dependency.
    """
    import hashlib
    import connection_hub.delegated_credentials.automation_access as core

    permitted = {
        "connection_hub.delegated_credentials.controls.project_person_composition": {
            "compose_with_project_held_control", "project_held_control"},
        "connection_hub.delegated_credentials.project_authorization": {
            "ProjectAuthorizationPort", "ViewerAuthority"},
        "connection_hub.delegated_credentials.project_identity_authorization": {"ProjectOperationRequest"},
        "connection_hub.delegated_credentials.project_identity_lifecycle": {"ProjectIdentityLifecycleError"},
        "connection_hub.delegated_credentials.project_invitation_access": {"ProjectInvitationControlLifecycle"},
        "connection_hub.delegated_credentials.project_invitation_binding": {"ProjectInvitationBindingResolver"},
        "connection_hub.delegated_credentials.project_person_access": {"ProjectPersonControlLifecycle"},
    }
    literal_hashes = {
        "d56746cb3172cf2e7ae14594a613f3f8bb488ba471b0c3e5c9abbbc9bb1e5dad",
        "a540b93b209946bd580ec329d27ba83247c9f6cb726e6e55cac3c52f4edc7846",
        "14898462758d22d38aacc8ba3297704724549f4e1d649e14171f6a0900c627f0",
        "06ed40204c58d75ab21d9ccb9696d36729917dbe5ba09a56aa3365a89d2efa60",
        "923dd485b40ca3018d1abe3e4bac0a6339fe06c939d81ccc6115cf2a6726f011",
        "7602f9acfd279c8b1c2a30eec0f9d42998fe92b98aebf8bcd244b26f13ed2caa",
    }
    tree = ast.parse(Path(core.__file__).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if "project_" in module or "board" in module:
                assert module in permitted
                assert {alias.name for alias in node.names} <= permitted[module]
        if isinstance(node, ast.Import):
            assert not any("project_" in alias.name or "board" in alias.name for alias in node.names)
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if any(part in node.value for part in ("project_", "work:project:", "project.")):
                assert hashlib.sha256(node.value.encode()).hexdigest() in literal_hashes
            assert "problem-board" not in node.value and "problem_board" not in node.value


def test_new_issuer_modules_have_no_domain_policy_or_module_level_mutable_state():
    import connection_hub.delegated_credentials.issuer_gate as gate
    import connection_hub.delegated_credentials.remote_issuer as remote
    root = Path(gate.__file__).resolve().parents[5]
    app_adapter = root / "apps/connection-hub@1-0/services/issuer_authorities.py"
    # App source is present in the source tree; installed package tests still
    # inspect both portable modules without requiring a bundled application.
    paths = [Path(gate.__file__), Path(remote.__file__)]
    if app_adapter.exists():
        paths.append(app_adapter)
    for path in paths:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert not any(part in (node.module or "") for part in ("project_", "board"))
            if isinstance(node, ast.Import):
                assert not any("project_" in alias.name or "board" in alias.name for alias in node.names)
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert not any(part in node.value for part in (
                    "problem-board", "problem_board", "work:project:", "project.cards.", "project_card_issuer"))
        for statement in tree.body:
            if isinstance(statement, (ast.Assign, ast.AnnAssign)):
                value = statement.value
                # Constants and typing aliases are fine; caches, mutex maps,
                # reservations and registry instances are not module state.
                assert not isinstance(value, (ast.Dict, ast.List, ast.Set, ast.Call))
