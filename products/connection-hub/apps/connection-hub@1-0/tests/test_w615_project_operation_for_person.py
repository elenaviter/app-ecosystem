"""W615: a project operation authorized for the PROVEN Telegram sender, never the request's caller.

Problem Board's webhook runs under one bound caller. A member's reply must be
decided by that member's own live Cards, so the board proves the member with
its own service key (purpose: Telegram, single use), names them explicitly,
and the Hub accepts it only when that member holds exactly the sending
Telegram edge. The decision is the existing per-person evaluation; here a
recording service shows it is asked for the proven person and nobody else.
"""
from __future__ import annotations

import asyncio
import secrets
from pathlib import Path

import pytest

from connection_hub.hub.edges import ConnectionEdgeStore
from kdcube_ai_app.apps.chat.sdk.runtime.dynamic_module_loader import load_dynamic_module_for_path

SECRET = "s" * 40
SERVICE = "problem-board"
NOW = 1_800_000_000
PERSON, OTHER, TELEGRAM = "cognito:member-1", "cognito:member-2", "700000001"
PROJECT = "work:project:quickstart"
RESOURCE = "https://board.example.test/mcp/problem_board"


class _Redis:
    def __init__(self):
        self.values = {}

    async def set(self, key, value, *, nx=False, ex=None):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True


class _Service:
    """Stands for the existing per-person evaluation; records whom it decided for."""

    def __init__(self, allowed=True):
        self.allowed, self.calls = allowed, []

    async def project_operation_authorize(self, user, **request):
        self.calls.append((dict(user), dict(request)))
        return {"ok": True, "schema": "connection-hub.project-operation-authorization.v1", "allowed": self.allowed,
                "request": {"person_subject": user["user_id"], **{k: request[k] for k in ("project_ref", "resource",
                                                                                     "operation")},
                            "required_grants": list(request["required_grants"])}}


@pytest.fixture
def world(tmp_path, monkeypatch):
    _name, module = load_dynamic_module_for_path(Path(__file__).resolve().parents[1] / "entrypoint.py")
    instance = module.ConnectionHubEntrypoint.__new__(module.ConnectionHubEntrypoint)
    instance.redis = _Redis()
    store = ConnectionEdgeStore(tmp_path)
    state = {"service": _Service(), "resources": ["urn:kdcube:project-operation:telegram"]}
    monkeypatch.setattr(module, "_edge_store", lambda _e: store)
    monkeypatch.setattr(module, "_identity_config", lambda _e: {})
    monkeypatch.setattr(module, "_connections_config", lambda _e: {
        "delegated_credentials": {"admission": {"enabled": True, "max_clock_skew_seconds": 300, "services": {
            SERVICE: {"secret_ref": "identity.lookup_secret", "resources": state["resources"]}}}}})

    async def rows(_e):
        return []

    monkeypatch.setattr(module, "_cached_authenticator_rows", rows)
    monkeypatch.setattr(module, "matching_authenticator_rows",
                        lambda _cfg, provider, **_kw: [{"provider": provider, "enabled": True}])

    async def secret(_e, *, secret_path, **_kw):
        return {"identity.lookup_secret": SECRET}.get(secret_path, "")

    async def service(_e, request):
        state["request"] = request
        return state["service"]

    monkeypatch.setattr(module, "_bundle_secret_value", secret)
    monkeypatch.setattr(module, "_automation_access_service", service)
    return module, instance, store, state


def _payload(module, *, person=PERSON, provider_subject=TELEGRAM, operation="control.enqueue", sign=None,
             timestamp=NOW, secret=SECRET):
    payload = {"project_ref": PROJECT, "person_subject": person, "provider": "telegram",
               "provider_subject": provider_subject, "resource": RESOURCE, "operation": operation,
               "required_grants": ["work:operate"], "request_resource": "", "surface": "application"}
    nonce = secrets.token_hex(16)
    signed = module.person_operation_request({**payload, **(sign or {})})
    signature = module.person_operation_signature(secret=secret, service_id=SERVICE, timestamp=str(timestamp),
                                                  nonce=nonce, request=signed)
    return {**payload, "service_proof": {"service_id": SERVICE, "timestamp": str(timestamp), "nonce": nonce,
                                         "signature": signature}}


def _run(module, instance, payload, request=None):
    return asyncio.run(module._project_operation_authorize_for_person(instance, payload, request=request, now=NOW))


def _edges(store, mapping):
    """person -> Telegram subjects, in the real edge store."""
    for person, subjects in mapping.items():
        for subject in subjects:
            store.upsert_edge(from_provider="telegram", from_subject=subject, to_user_id=person)


def test_the_proven_member_is_decided_by_their_own_cards_never_the_caller(world, monkeypatch):
    module, instance, store, state = world
    _edges(store, {PERSON: [TELEGRAM]})
    caller = object()  # the webhook's bound request: passed on for the SDK config, never as the person
    answer = _run(module, instance, _payload(module), request=caller)
    assert answer["ok"] is True and answer["allowed"] is True, answer
    assert answer["person_subject"] == PERSON and answer["request"]["person_subject"] == PERSON
    ((user, request),) = state["service"].calls
    assert user == {"user_id": PERSON}
    assert request["operation"] == "control.enqueue" and request["required_grants"] == ["work:operate"]


def test_a_member_whose_cards_lack_the_operation_gets_the_refusal(world, monkeypatch):
    module, instance, store, state = world
    state["service"] = _Service(allowed=False)
    _edges(store, {PERSON: [TELEGRAM]})
    answer = _run(module, instance, _payload(module))
    assert answer["allowed"] is False and answer["person_subject"] == PERSON


@pytest.mark.parametrize("case", ["unlinked", "other_telegram", "another_persons_subject", "two_telegram_edges"])
def test_a_sender_that_is_not_exactly_that_persons_telegram_edge_is_refused(world, monkeypatch, case):
    module, instance, store, state = world
    edges = {"unlinked": {}, "other_telegram": {PERSON: ["700000099"]},
             "another_persons_subject": {OTHER: [TELEGRAM]},
             "two_telegram_edges": {PERSON: [TELEGRAM, "700000099"]}}[case]
    _edges(store, edges)
    answer = _run(module, instance, _payload(module))
    assert answer == {"ok": False, "error": "project_operation_sender_not_linked", "status": 403}
    assert state["service"].calls == []  # nobody's Cards were consulted


def test_a_replayed_proof_is_refused(world, monkeypatch):
    module, instance, store, state = world
    _edges(store, {PERSON: [TELEGRAM]})
    payload = _payload(module)
    assert _run(module, instance, payload)["allowed"] is True
    assert _run(module, instance, payload) == {"ok": False, "error": "project_operation_for_person_proof_replayed",
                                               "status": 403}
    assert len(state["service"].calls) == 1


@pytest.mark.parametrize("case", ["other_person_signed", "other_operation_signed", "wrong_key", "stale",
                                  "no_proof", "not_permitted", "missing_field"])
def test_a_proof_for_anything_else_is_refused_before_any_card_is_read(world, monkeypatch, case):
    module, instance, store, state = world
    _edges(store, {PERSON: [TELEGRAM], OTHER: ["700000002"]})
    payload = {
        "other_person_signed": lambda: _payload(module, sign={"person_subject": OTHER}),
        "other_operation_signed": lambda: _payload(module, sign={"operation": "project.cards.manage"}),
        "wrong_key": lambda: _payload(module, secret="w" * 40),
        "stale": lambda: _payload(module, timestamp=NOW - 3600),
        "no_proof": lambda: {k: v for k, v in _payload(module).items() if k != "service_proof"},
        "not_permitted": lambda: _payload(module),
        "missing_field": lambda: {**_payload(module), "person_subject": ""},
    }[case]()
    if case == "not_permitted":
        state["resources"][:] = ["urn:kdcube:identity:provider-subject:telegram"]  # the W609 purpose only
    answer = _run(module, instance, payload)
    assert answer["ok"] is False and answer["error"] == {
        "other_person_signed": "project_operation_for_person_proof_invalid",
        "other_operation_signed": "project_operation_for_person_proof_invalid",
        "wrong_key": "project_operation_for_person_proof_invalid",
        "stale": "project_operation_for_person_proof_invalid",
        "no_proof": "project_operation_for_person_requires_service_proof",
        "not_permitted": "project_operation_for_person_not_permitted",
        "missing_field": "project_operation_for_person_request_invalid",
    }[case], (case, answer)
    assert state["service"].calls == []


def test_the_signature_is_domain_separated_from_the_w609_lookup(world):
    module, *_ = world
    request = module.person_operation_request({"project_ref": PROJECT, "person_subject": PERSON,
        "provider": "telegram", "provider_subject": TELEGRAM, "resource": RESOURCE, "operation": "control.enqueue",
        "required_grants": ["work:operate"]})
    ours = module.person_operation_signature(secret=SECRET, service_id=SERVICE, timestamp=str(NOW), nonce="n",
                                             request=request)
    lookup = module.identity_subject_lookup_signature(secret=SECRET, service_id=SERVICE, timestamp=str(NOW),
                                                      nonce="n", platform_user_id=PERSON, provider="telegram")
    assert ours != lookup and len(ours) == 64
    assert module.PERSON_OPERATION_AUTHORIZE in module.CSRF_EXEMPT_POST_OPERATION_ALIASES
