"""W569: the pair lifecycle through the SDK's real mounted operations dispatch.

The existing handler tests call ``issuer_managed_lifecycle_apply`` directly with
a hand-made ``comm_context``. Here the request goes through KDCube's actual
``integrations._call_bundle_op_inner``. That applies the endpoint's declared
user_types, single-use CSRF for ambient cookie sessions, the implicit
``user_id``/``fingerprint`` kwargs, and builds ``comm_context`` from the
session. The real Connection Hub entrypoint class handles the request. Stubbed,
as in the SDK's own operations-auth tests: bundle resolution and loading (the
real class, constructed without its runtime), settings, and the automation
service the handler calls, which records the actor the handler binds. The
service's own pair behaviour is covered against real storage and Redis
elsewhere in W569.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.datastructures import Headers

from connection_hub.delegated_credentials.issuer_gate import change_digest
from kdcube_ai_app.apps.chat.proc.rest.integrations import integrations, operation_csrf
from kdcube_ai_app.apps.chat.sdk.runtime.dynamic_module_loader import load_dynamic_module_for_path

ENTRYPOINT = Path(__file__).resolve().parents[1] / "entrypoint.py"


def _body():
    targets = [{"owner_subject": f"owner-{i}", "access_id": f"card-{i}", "expected_card_revision": 1,
                "expected_authority_fingerprint": "a" * 64, "issuer_kind": f"opaque-{i}", "issuer_ref": "opaque"}
               for i in range(2)]
    return {"context_ref": "reserved-context", "request_id": "recorded-request", "action": "revoke",
            "change_digest": change_digest({"action": "revoke", "targets": targets}), "targets": targets}


def _session(*, user_type="registered", user_id="human-1", username="human", authority=None):
    return SimpleNamespace(
        session_id="session-1", user_type=SimpleNamespace(value=user_type), user_id=user_id,
        username=username, email=None, fingerprint="fp-real", roles=["kdcube:role:member"],
        permissions=["chat.use"], identity_authority=dict(authority or {}),
        request_context=SimpleNamespace(user_timezone="UTC", user_utc_offset_min=0),
    )


class _Redis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def setex(self, key, _ttl, value):
        self.values[key] = value
        return True

    async def eval(self, _script, _numkeys, key):
        return self.values.pop(key, None)


def _request(*, cookie=False, csrf_token="", bearer=True):
    headers = {}
    if csrf_token:
        headers[operation_csrf.OPERATION_CSRF_HEADER] = csrf_token
    if bearer:
        headers["Authorization"] = "Bearer synthetic-non-ambient"
    return SimpleNamespace(method="POST", headers=Headers(headers),
                           cookies={"__Secure-LATC": "ambient-session"} if cookie else {},
                           state=SimpleNamespace(),
                           app=SimpleNamespace(state=SimpleNamespace(redis_async=_Redis(), pg_pool=object())))


class _RecordingService:
    def __init__(self) -> None:
        self._issuers = object()
        self.bound = []
        self.applied = []

    def bind_issuer_registry(self, issuers, *, actor_subject):
        self.bound.append(actor_subject)

    async def issuer_managed_lifecycle_apply(self, payload):
        self.applied.append(payload)
        return {"ok": True, "status": 200}


@pytest.fixture
def mounted(monkeypatch):
    module = load_dynamic_module_for_path(ENTRYPOINT)[1]
    service = _RecordingService()

    async def automation_service(_entrypoint, _request):
        return service

    monkeypatch.setattr(module, "_automation_access_service", automation_service)

    async def resolve(*args, **kwargs):
        return SimpleNamespace(id=kwargs.get("bundle_id") or "connection-hub@1-0", path=str(ENTRYPOINT.parent),
                               module="entrypoint", singleton=False)

    async def workflow_instance(spec, wf_config, comm_context=None, redis=None, pg_pool=None):
        instance = module.ConnectionHubEntrypoint.__new__(module.ConnectionHubEntrypoint)
        instance.comm_context = comm_context  # exactly what the SDK built from the session
        return instance, module

    monkeypatch.setattr(integrations, "get_settings", lambda: SimpleNamespace(
        OPENAI_API_KEY=None, ANTHROPIC_API_KEY=None, TENANT="tenant-a", PROJECT="project-a"))
    monkeypatch.setattr(operation_csrf, "get_settings", lambda: SimpleNamespace(AUTH=SimpleNamespace(
        ID_TOKEN_HEADER_NAME="X-ID-Token", AUTH_TOKEN_COOKIE_NAME="__Secure-LATC",
        ID_TOKEN_COOKIE_NAME="__Secure-LITC", MASQUERADED_TOKEN_COOKIE_NAME="__Secure-LMTC")))
    monkeypatch.setattr(integrations, "_resolve_bundle_spec_from_runtime", resolve)
    monkeypatch.setattr(integrations, "create_workflow_config", lambda _cfg: SimpleNamespace(ai_bundle_spec=None))
    monkeypatch.setattr(integrations, "get_workflow_instance_async", workflow_instance)
    monkeypatch.setattr(integrations, "store_get_bundle_props_from_authority", lambda **kwargs: {})
    return service


async def _apply(session, data, request=None):
    return await integrations._call_bundle_op_inner(
        tenant="tenant-a", project="project-a", bundle_id=None,
        payload=integrations.BundleSuggestionsRequest(bundle_id="connection-hub@1-0", data=data),
        request=request or _request(), operation="issuer_managed_lifecycle_apply", route="operations",
        session=session)


def _result(response):
    return response.get("issuer_managed_lifecycle_apply", response)


@pytest.mark.asyncio
@pytest.mark.parametrize("user_type", ["registered", "privileged"])
async def test_a_platform_human_reaches_the_pair_service_bound_to_the_session_user(mounted, user_type):
    response = await _apply(_session(user_type=user_type), _body())

    assert _result(response) == {"ok": True, "status": 200}, response
    assert mounted.bound == ["human-1"]
    assert mounted.applied == [_body()]


@pytest.mark.asyncio
async def test_forged_user_id_and_fingerprint_in_the_body_never_select_the_actor(mounted):
    response = await _apply(_session(), {**_body(), "user_id": "owner-0", "fingerprint": "forged"})

    assert _result(response) == {"ok": True, "status": 200}, response
    assert mounted.bound == ["human-1"], "the actor is the authenticated session user, not the body"
    assert mounted.applied == [_body()], "the forged metadata never reaches the DTO"


@pytest.mark.asyncio
@pytest.mark.parametrize("session,status,detail", [
    (_session(user_type="anonymous", user_id="anonymous"), 401, "User is required."),
    (_session(user_type="external", user_id="owner-0", username="integration:agent:owner-0",
              authority={"authority_id": "delegated_client", "grantor_user_id": "owner-0"}), 403,
     "Bundle operation issuer_managed_lifecycle_apply is not visible to this user"),
], ids=["anonymous", "external-owner-equal"])
async def test_a_non_human_session_is_refused_by_the_mounted_dispatch_before_the_handler(mounted, session, status, detail):
    with pytest.raises(HTTPException) as refused:
        await _apply(session, _body())

    assert (refused.value.status_code, refused.value.detail) == (status, detail)
    assert mounted.bound == [] and mounted.applied == []


@pytest.mark.asyncio
@pytest.mark.parametrize("authority", [
    {"authority_id": "delegated_client", "grantor_user_id": "owner-0"},
    {"delegated_card_binding": {"access_id": "agent-card"}},
    {"delegate_identity": "agent-1"},
], ids=["delegated-client-grantor-equals-owner", "card-binding", "delegate-identity"])
async def test_a_registered_session_carrying_delegated_authority_is_refused_by_the_handler(mounted, authority):
    response = await _apply(_session(user_id="owner-0", authority=authority), _body())

    assert _result(response) == {"ok": False, "error": "issuer_lifecycle_requires_platform_human", "status": 403}
    assert mounted.bound == [] and mounted.applied == []


@pytest.mark.asyncio
async def test_an_ambient_cookie_session_without_its_single_use_csrf_token_is_refused(mounted):
    with pytest.raises(HTTPException) as refused:
        await _apply(_session(), _body(), request=_request(cookie=True, bearer=False))

    assert (refused.value.status_code, refused.value.detail) == (403, "Operation CSRF token is missing, expired, or invalid.")
    assert mounted.bound == [] and mounted.applied == []


@pytest.mark.asyncio
async def test_a_nested_dto_extra_is_refused_after_identity_and_before_the_service(mounted):
    response = await _apply(_session(), {"data": {**_body(), "actor_subject": "owner-0"}})

    assert _result(response) == {"ok": False, "error": "issuer_lifecycle_request_invalid", "status": 400}, response
    assert mounted.applied == []
