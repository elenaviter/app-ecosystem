"""W502: the two peer endpoints are CSRF-exempt because a browser session never authenticates them.

``card_transaction_participant`` and ``card_census_read`` are public POSTs
authenticated only by the peer's admission proof and a single-use nonce. A
request carrying a signed-in browser session but no valid peer proof is
refused, and never reaches the decision store.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from kdcube_ai_app.apps.chat.sdk.runtime.dynamic_module_loader import load_dynamic_module_for_path

from connection_hub.delegated_credentials.cards.participant_descriptor import BuiltCallers
from connection_hub.delegated_credentials.cards.participant_operation import ParticipantCaller


def _module():
    return load_dynamic_module_for_path(Path(__file__).resolve().parents[1] / "entrypoint.py")[1]


def _browser_request() -> Request:
    return Request({"type": "http", "http_version": "1.1", "method": "POST", "scheme": "https",
                    "path": "/api/integrations/bundles/t/p/connection-hub@1-0/public/x", "raw_path": b"",
                    "query_string": b"", "client": ("127.0.0.1", 1), "server": ("hub.example.test", 443),
                    "headers": [(b"host", b"hub.example.test"), (b"cookie", b"session=signed-in-user"),
                                (b"x-csrf-token", b"browser-token")]})


def _collection_section(collection_id):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def held():
        yield

    return held()


class _NeverStore:
    """W661: piece 1's Card version store; an unauthenticated request never reaches it."""

    def __getattr__(self, name):
        raise AssertionError("an unauthenticated request reached the Card version store")


class _Nonces:
    async def set(self, key, value, *, ex, nx):
        return True


@pytest.fixture
def entrypoint(monkeypatch):
    module = _module()
    caller = ParticipantCaller(service_id="problem-board", request_secret="r" * 40, receipt_secret="s" * 40,
                               receipt_signer_id="connection-hub@1-0", audience="problem-board@1-0",
                               hub_resource="connection-hub@1-0", bind=None, census_scope_prefix="work:project:")

    async def persistence(_entrypoint, _redis):
        # W651: registration seals under the Card service's collection lock (collection_section).
        return SimpleNamespace(card_store=object(), card_service=SimpleNamespace(collection_section=_collection_section),
                               card_versions=_NeverStore())

    async def callers(_entrypoint, _persistence):
        return BuiltCallers(callers={"problem-board": caller}, authorities={})

    async def never(*args, **kwargs):
        raise AssertionError("an unauthenticated request reached the decision store")

    monkeypatch.setattr(module, "_delegated_card_persistence", persistence)
    monkeypatch.setattr(module, "_card_participant_callers", callers)
    monkeypatch.setattr(module, "_card_decision_store", never)
    monkeypatch.setattr(module, "_delegated_authority_config", lambda _e: SimpleNamespace(uses_postgresql=True))
    monkeypatch.setattr(module, "_delegated_catalog_store", lambda _e: None)
    monkeypatch.setattr(module, "_runtime_tenant_project", lambda _e: ("t", "p"))

    async def host(_entrypoint, _request):
        return object()

    async def never_plan(*args, **kwargs):
        raise AssertionError("an unauthenticated request reached the planner")

    monkeypatch.setattr(module, "_automation_access_service", host)
    monkeypatch.setattr(module, "plan_card_lifecycle", never_plan)
    instance = module.ConnectionHubEntrypoint.__new__(module.ConnectionHubEntrypoint)
    instance.redis, instance.pg_pool, instance.bundle_props = _Nonces(), object(), {}
    return instance


def _forged(schema, fields):
    return {"schema": schema, "request_echo": os.urandom(16).hex(), **fields,
            "service_proof": {"service_id": "problem-board", "timestamp": "1800000000",
                              "nonce": os.urandom(12).hex(), "signature": "A" * 43}}


@pytest.mark.asyncio
async def test_w502_peer_endpoints_ignore_the_browser_session(entrypoint):
    census = await entrypoint.card_census_read(data={"scope": "work:project:one"}, request=_browser_request())
    assert census["ok"] is False and census["error"]["code"] == "card_census_request_invalid"
    forged = _forged("card-census-read.v1", {"scope": "work:project:one", "persons": ["user:a"],
                                             "include_catalog": False})
    census = await entrypoint.card_census_read(data=forged, request=_browser_request())
    assert census["ok"] is False and census["error"]["code"] == "card_participant_unauthenticated"
    assert "census_answer" not in census

    participant = await entrypoint.card_transaction_participant(data={"action": "prepare"},
                                                                request=_browser_request())
    assert participant["ok"] is False and participant["error"]["code"] == "card_participant_request_invalid"
    forged = _forged("card-transaction-participant.v1", {"action": "read_pending", "scope": "work:project:one",
                                                         "transaction_id": "a" * 64, "decision": None,
                                                         "limit": None, "cursor": None})
    participant = await entrypoint.card_transaction_participant(data=forged, request=_browser_request())
    assert participant["ok"] is False and participant["error"]["code"] == "card_participant_unauthenticated"
    assert "participant_answer" not in participant


@pytest.mark.asyncio
async def test_w578_lifecycle_plan_endpoint_ignores_the_browser_session(entrypoint):
    """W578: card_lifecycle_plan is a peer endpoint like card_census_read."""
    module = _module()
    assert "card_lifecycle_plan" in module.CSRF_EXEMPT_PUBLIC_POST_ALIASES
    plan = await entrypoint.card_lifecycle_plan(data={"scope": "work:project:one"}, request=_browser_request())
    assert plan["ok"] is False and plan["error"]["code"] == "card_plan_request_invalid"
    forged = _forged("card-lifecycle-plan-request.v1", {
        "scope": "work:project:one", "actor_subject": "user:a", "actor_kind": "caller", "request_id": "r-1",
        "creations": [{"ref": "c", "kind": "project_person_control", "identity": {"target_subject": "user:a"},
                       "selection": {}, "parent": None}],
        "updates": []})
    plan = await entrypoint.card_lifecycle_plan(data=forged, request=_browser_request())
    assert plan["ok"] is False and plan["error"]["code"] == "card_participant_unauthenticated"
    assert "plan_answer" not in plan


@pytest.mark.asyncio
async def test_w661_card_version_endpoint_ignores_the_browser_session(entrypoint):
    """W661: card_version is a peer endpoint like card_lifecycle_plan."""
    module = _module()
    assert "card_version" in module.CSRF_EXEMPT_PUBLIC_POST_ALIASES
    answer = await entrypoint.card_version(data={"op": "publish"}, request=_browser_request())
    assert answer["ok"] is False and answer["error"]["code"] == "card_version_request_invalid"
    forged = _forged("card-version-request.v1", {
        "op": "publish", "scope": "work:project:one", "txn": "t" * 40, "request_id": None, "at": None, "catalog": None,
        "actor_subject": None, "actor_kind": None, "delegable_grants": None, "project_control": None,
        "creations": None, "updates": None, "links": None})
    answer = await entrypoint.card_version(data=forged, request=_browser_request())
    assert answer["ok"] is False and answer["error"]["code"] == "card_participant_unauthenticated"
    assert "participant_answer" not in answer


@pytest.mark.asyncio
async def test_w502_read_collection_registration_ignores_the_browser_session(entrypoint):
    """Lane D: card_read_collection_register is a peer endpoint like card_census_read."""
    module = _module()
    assert "card_read_collection_register" in module.CSRF_EXEMPT_PUBLIC_POST_ALIASES
    answer = await entrypoint.card_read_collection_register(data={"scope": "work:project:one"},
                                                            request=_browser_request())
    assert answer["ok"] is False and answer["error"]["code"] == "card_read_collection_request_invalid"
    forged = _forged("card-read-collection-register.v1", {
        "scope": "work:project:one", "persons": ["user:a"], "exclude": [], "actor_subject": "a", "request_id": "r-1",
        "deadline": 1_800_000_300})
    answer = await entrypoint.card_read_collection_register(data=forged, request=_browser_request())
    assert answer["ok"] is False and answer["error"]["code"] == "card_participant_unauthenticated"
    assert "collection_answer" not in answer


@pytest.mark.asyncio
async def test_w651_registration_receives_the_card_services_collection_lock(entrypoint, monkeypatch):
    """The production endpoint passes persistence.card_service.collection_section into the operation."""
    module = type(entrypoint).card_read_collection_register.__globals__  # the fixture instance's own module
    built = []
    real = module["CardReadCollectionOperation"]

    def recording(**kwargs):
        built.append(kwargs)
        return real(**kwargs)

    monkeypatch.setitem(module, "CardReadCollectionOperation", recording)
    await entrypoint.card_read_collection_register(data={"scope": "work:project:one"}, request=_browser_request())
    assert len(built) == 1 and built[0]["collection_lock"] is _collection_section


@pytest.mark.asyncio
async def test_w651_registration_without_a_collection_lock_is_unavailable(entrypoint, monkeypatch):
    """Never seal without the lock retention takes: no provider, a 503 before any request handling."""
    module = type(entrypoint).card_read_collection_register.__globals__  # the fixture instance's own module

    async def persistence(_entrypoint, _redis):
        return SimpleNamespace(card_store=object(), card_service=object())

    def never_built(**kwargs):
        raise AssertionError("an operation was built without the collection lock")

    monkeypatch.setitem(module, "_delegated_card_persistence", persistence)
    monkeypatch.setitem(module, "CardReadCollectionOperation", never_built)
    answer = await entrypoint.card_read_collection_register(data={"scope": "work:project:one"},
                                                            request=_browser_request())
    assert answer == {"ok": False, "status": 503, "error": {"code": "card_participant_unavailable"}}


@pytest.mark.asyncio
async def test_w578_an_authenticated_plan_reaches_the_callers_own_host_and_the_planner(monkeypatch):
    import time

    from connection_hub.delegated_credentials.admission import AdmissionRequest, sign_admission_request
    from connection_hub.delegated_credentials.cards.lifecycle_plan_operation import (
        REQUEST_SCHEMA, plan_request_digest,
    )
    from connection_hub.delegated_credentials.project_authorization import (
        LifecyclePlanAuthorization, ProjectAuthorizationDecision,
    )

    module = _module()
    asked, planned, host = [], [], object()

    class _Board:
        async def authorize_lifecycle_plan(self, request):
            asked.append(request)
            return LifecyclePlanAuthorization(request=request, decisions=tuple(
                (step.ref, ProjectAuthorizationDecision.allow(request.step_request(step))) for step in request.steps))

    async def planner(given_host, **kwargs):
        planned.append((given_host, kwargs))
        return {"ok": True, "plan": {"candidate_value": {}, "participant_input": {}, "reads": [],
                                     "catalog_digest": "0" * 64}}

    async def host_for(_entrypoint, _request):
        return host

    caller = ParticipantCaller(service_id="problem-board", request_secret="r" * 40, receipt_secret="s" * 40,
                               receipt_signer_id="connection-hub@1-0", audience="problem-board@1-0",
                               hub_resource="connection-hub@1-0", bind=None, plan_scope_prefix="work:project:",
                               plan_authorization=_Board())

    async def persistence(_entrypoint, _redis):
        return SimpleNamespace(card_store=object(), card_service=object())

    async def callers(_entrypoint, _persistence):
        return BuiltCallers(callers={"problem-board": caller}, authorities={})

    monkeypatch.setattr(module, "_delegated_card_persistence", persistence)
    monkeypatch.setattr(module, "_card_participant_callers", callers)
    monkeypatch.setattr(module, "_runtime_tenant_project", lambda _e: ("t", "p"))
    monkeypatch.setattr(module, "_automation_access_service", host_for)
    monkeypatch.setattr(module, "plan_card_lifecycle", planner)
    instance = module.ConnectionHubEntrypoint.__new__(module.ConnectionHubEntrypoint)
    instance.redis, instance.pg_pool, instance.bundle_props = _Nonces(), object(), {}

    data = {"schema": REQUEST_SCHEMA, "request_echo": os.urandom(16).hex(), "scope": "work:project:one",
            "actor_subject": "user:a", "actor_kind": "caller", "request_id": "r-1",
            "creations": [{"ref": "c", "kind": "project_person_control", "identity": {"target_subject": "user:a"},
                           "selection": {}, "parent": None}], "updates": []}
    proof = {"service_id": "problem-board", "timestamp": str(int(time.time())), "nonce": os.urandom(12).hex()}
    proof["signature"] = sign_admission_request(
        secret="r" * 40, **proof, delegated_token=f"{REQUEST_SCHEMA}:{data['request_echo']}",
        request=AdmissionRequest(resource="connection-hub@1-0", operation="card_lifecycle_plan",
                                 invocation_id=data["request_echo"], request_digest=plan_request_digest(data),
                                 approval_context={"protocol": REQUEST_SCHEMA}))
    answer = await instance.card_lifecycle_plan(data={**data, "service_proof": proof}, request=_browser_request())
    assert answer["ok"] is True and answer["plan_answer"]["result"]["kind"] == "plan"
    [request] = asked
    assert request.request_digest == plan_request_digest(data) and request.project_ref == "work:project:one"
    [(given_host, kwargs)] = planned
    assert given_host is host and kwargs["authorization"].request == request
