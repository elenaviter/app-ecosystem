"""W609: the service-only reverse lookup (platform user -> one provider subject).

Only a registered admission service whose resources name the provider may
read it, proven by its HMAC peer proof; the request's user session is never
consulted. Minimum output, exact typed match, single-use proof, no subject
in the audit line, no cache.
"""
from __future__ import annotations

import logging
import secrets
import time
from pathlib import Path

import pytest

from connection_hub.delegated_credentials.admission import AdmissionRequest, sign_admission_request
from connection_hub.hub.edges import ConnectionEdgeStore
from kdcube_ai_app.apps.chat.sdk.runtime.dynamic_module_loader import load_dynamic_module_for_path

SECRET = "s" * 40
OTHER_SECRET = "o" * 40  # another registered service's own key
SERVICE = "problem-board"
NOW = 1_800_000_000


class _Redis:
    def __init__(self):
        self.values = {}

    async def set(self, key, value, *, nx=False, ex=None):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True


def _module():
    _name, module = load_dynamic_module_for_path(Path(__file__).resolve().parents[1] / "entrypoint.py")
    return module


@pytest.fixture
def world(tmp_path, monkeypatch):
    module = _module()
    instance = module.ConnectionHubEntrypoint.__new__(module.ConnectionHubEntrypoint)
    instance.redis = _Redis()
    store = ConnectionEdgeStore(tmp_path)
    state = {"authenticators": True, "services": {
        SERVICE: {"secret_ref": "identity.lookup_secret", "resources": ["urn:kdcube:identity:provider-subject:telegram"]},
        "other-service": {"secret_ref": "identity.other_service_secret",
                          "resources": ["urn:kdcube:identity:provider-subject:slack"]},
    }}
    monkeypatch.setattr(module, "_edge_store", lambda _e: store)
    monkeypatch.setattr(module, "_identity_config", lambda _e: {})
    monkeypatch.setattr(module, "_connections_config", lambda _e: {
        "delegated_credentials": {"admission": {"enabled": True, "max_clock_skew_seconds": 300,
                                                 "services": state["services"]}}})

    async def rows(_e):
        return []

    monkeypatch.setattr(module, "_cached_authenticator_rows", rows)
    monkeypatch.setattr(module, "matching_authenticator_rows",
                        lambda _cfg, provider, **_kw: [{"provider": provider, "enabled": True}]
                        if state["authenticators"] and provider == "telegram" else [])

    async def secret(_e, *, secret_path, **_kw):
        return {"identity.lookup_secret": SECRET, "identity.other_service_secret": OTHER_SECRET}.get(secret_path, "")

    monkeypatch.setattr(module, "_bundle_secret_value", secret)
    return module, instance, store, state


def _proof(module, user, provider="telegram", *, service=SERVICE, secret=SECRET, operation=None,
           timestamp=NOW, digest_user=None):
    nonce = secrets.token_hex(16)
    request = AdmissionRequest(
        resource=module.identity_subject_lookup_resource(provider),
        operation=operation or module.IDENTITY_SUBJECT_LOOKUP_OPERATION, invocation_id=nonce,
        request_digest=module.identity_subject_lookup_digest(platform_user_id=digest_user or user, provider=provider))
    signature = sign_admission_request(secret=secret, service_id=service, timestamp=str(timestamp), nonce=nonce,
                                       delegated_token=module.IDENTITY_SUBJECT_LOOKUP_TOKEN, request=request)
    return {"service_id": service, "timestamp": str(timestamp), "nonce": nonce, "signature": signature}


async def _ask(module, instance, user, provider="telegram", proof=None):
    return await module._identity_provider_subject_resolve(
        instance, {"platform_user_id": user, "provider": provider, "service_proof": proof}, now=NOW)


@pytest.mark.asyncio
@pytest.mark.parametrize("user", ["cognito:7a1e2b3c-0000-4000-8000-00000000abcd", "42d5a4e4-0000-4000-8000-00000000beef"])
async def test_a_proven_service_reads_exactly_one_subject_for_either_id_form(world, user):
    module, instance, store, _ = world
    store.upsert_edge(from_provider="telegram", from_subject="100200300", to_user_id=user)
    store.upsert_edge(from_provider="slack", from_subject="U-other", to_user_id=user)
    store.upsert_edge(from_provider="telegram", from_subject="999", to_user_id="someone-else")
    answer = await _ask(module, instance, user, proof=_proof(module, user))
    assert answer == {"ok": True, "provider": "telegram", "provider_subject": "100200300"}  # nothing else


@pytest.mark.asyncio
async def test_without_a_valid_service_proof_nothing_is_read(world):
    module, instance, store, _ = world
    user = "cognito:7a1e2b3c-0000-4000-8000-00000000abcd"
    store.upsert_edge(from_provider="telegram", from_subject="100200300", to_user_id=user)
    cases = {
        "none": None,
        "wrong_secret": _proof(module, user, secret="x" * 40),
        "other_operation": _proof(module, user, operation="project_membership_list"),
        "stale": _proof(module, user, timestamp=NOW - 3600),
        "tampered_digest": _proof(module, user, digest_user="someone-else"),
        # (a) the key is bound to the service: another bundle's own secret cannot speak for problem-board
        "another_service_key": _proof(module, user, secret=OTHER_SECRET),
    }
    for name, proof in cases.items():
        answer = await _ask(module, instance, user, proof=proof)
        assert answer["ok"] is False and "provider_subject" not in answer, name
        assert answer["error"] in {"identity_lookup_requires_service_proof", "identity_lookup_proof_invalid"}, name


@pytest.mark.asyncio
async def test_a_proof_is_used_once(world):
    module, instance, store, _ = world
    user = "cognito:7a1e2b3c-0000-4000-8000-00000000abcd"
    store.upsert_edge(from_provider="telegram", from_subject="100200300", to_user_id=user)
    proof = _proof(module, user)
    assert (await _ask(module, instance, user, proof=proof))["ok"] is True
    assert (await _ask(module, instance, user, proof=proof))["error"] == "identity_lookup_proof_replayed"


@pytest.mark.asyncio
async def test_purpose_binding_registered_service_permitted_provider_and_an_enabled_authenticator(world):
    module, instance, store, state = world
    user = "cognito:7a1e2b3c-0000-4000-8000-00000000abcd"
    store.upsert_edge(from_provider="telegram", from_subject="100200300", to_user_id=user)
    unregistered = await _ask(module, instance, user, proof=_proof(module, user, service="unregistered-bundle"))
    other = await _ask(module, instance, user, proof=_proof(module, user, service="other-service", secret=OTHER_SECRET))
    slack = await _ask(module, instance, user, provider="slack", proof=_proof(module, user, provider="slack"))
    state["authenticators"] = False
    disabled = await _ask(module, instance, user, proof=_proof(module, user))
    for answer in (unregistered, other, slack, disabled):
        assert answer == {"ok": False, "error": "identity_lookup_not_permitted", "status": 403}


@pytest.mark.asyncio
async def test_unlinked_revoked_and_ambiguous_answer_without_a_subject(world):
    module, instance, store, _ = world
    user = "cognito:7a1e2b3c-0000-4000-8000-00000000abcd"
    assert (await _ask(module, instance, user, proof=_proof(module, user)))["error"] == "identity_not_linked"
    store.upsert_edge(from_provider="telegram", from_subject="100200300", to_user_id=user, status="revoked")
    assert (await _ask(module, instance, user, proof=_proof(module, user)))["error"] == "identity_not_linked"
    store.upsert_edge(from_provider="telegram", from_subject="111", to_user_id=user)
    store.upsert_edge(from_provider="telegram", from_subject="222", to_user_id=user)
    answer = await _ask(module, instance, user, proof=_proof(module, user))
    assert answer == {"ok": False, "error": "identity_lookup_ambiguous", "provider": "telegram"}


@pytest.mark.asyncio
async def test_the_id_is_typed_never_parsed_as_an_actor_and_the_session_is_ignored(world, monkeypatch):
    module, instance, store, _ = world
    user = "cognito:7a1e2b3c-0000-4000-8000-00000000abcd"
    store.upsert_edge(from_provider="telegram", from_subject="100200300", to_user_id=user)
    # Whoever the request's caller is must not matter, nor be consulted.
    monkeypatch.setattr(module, "_platform_user_id", lambda *_a, **_k: pytest.fail("session consulted"))
    monkeypatch.setattr(module, "_target_user_id", lambda *_a, **_k: pytest.fail("session consulted"))
    assert (await _ask(module, instance, user, proof=_proof(module, user)))["provider_subject"] == "100200300"
    # A near-miss id form is not mapped.
    other_form = "cognito_7a1e2b3c-0000-4000-8000-00000000abcd"
    assert (await _ask(module, instance, other_form, proof=_proof(module, other_form)))["error"] == "identity_not_linked"


@pytest.mark.asyncio
async def test_the_audit_line_never_contains_the_subject(world, caplog):
    module, instance, store, _ = world
    user = "cognito:7a1e2b3c-0000-4000-8000-00000000abcd"
    store.upsert_edge(from_provider="telegram", from_subject="100200300", to_user_id=user)
    with caplog.at_level(logging.INFO):
        assert (await _ask(module, instance, user, proof=_proof(module, user)))["ok"] is True
    lines = [r.getMessage() for r in caplog.records if "identity_provider_subject_resolve" in r.getMessage()]
    assert lines and all("100200300" not in line for line in lines)
    assert any("outcome=resolved" in line and SERVICE in line for line in lines)
