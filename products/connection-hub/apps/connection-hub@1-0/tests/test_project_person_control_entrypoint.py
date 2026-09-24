"""Project-person Control Card operations keep host authority out of payloads."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from fastapi import FastAPI
from starlette.requests import Request

from connection_hub.delegated_credentials.project_authorization import (
    PROJECT_PERSON_CONTROL_CREATE,
    ProjectAuthorizationRequest,
)

from kdcube_ai_app.apps.chat.proc.rest.integrations import integrations
from kdcube_ai_app.apps.chat.sdk.infra import (
    bundle_operations as bundle_operation_runtime,
)
from kdcube_ai_app.apps.chat.sdk.infra.bundle_operations import (
    BundleOperationCall,
    call_bundle_operation,
    make_local_bundle_operation_caller,
)
from kdcube_ai_app.apps.chat.sdk.runtime.dynamic_module_loader import (
    load_dynamic_module_for_path,
)
from kdcube_ai_app.infra.plugin.bundle_loader import api


def _entrypoint_module():
    bundle_root = Path(__file__).resolve().parents[1]
    _name, module = load_dynamic_module_for_path(bundle_root / "entrypoint.py")
    return module


class _Service:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def project_person_control_get(self, user, **kwargs):
        self.calls.append(("get", {"user": user, **kwargs}))
        return {"ok": True}

    async def project_person_control_create(self, user, **kwargs):
        self.calls.append(("create", {"user": user, **kwargs}))
        return {"ok": True}

    async def project_person_control_update(self, user, **kwargs):
        self.calls.append(("update", {"user": user, **kwargs}))
        return {"ok": True}

    async def project_person_control_revoke(self, user, **kwargs):
        self.calls.append(("revoke", {"user": user, **kwargs}))
        return {"ok": True}

    async def project_person_my_card_seed(self, user, **kwargs):
        self.calls.append(("seed", {"user": user, **kwargs}))
        return {"ok": True, "seeded": True}

    async def project_operation_authorize(self, user, **kwargs):
        self.calls.append(("authorize", {"user": user, **kwargs}))
        return {
            "ok": True,
            "allowed": True,
            "reason": "project_operation_allowed",
        }


@pytest.fixture()
def entrypoint(monkeypatch):
    module = _entrypoint_module()
    service = _Service()

    async def _access_service(*_args, **_kwargs):
        return service

    monkeypatch.setattr(module, "_automation_access_service", _access_service)
    monkeypatch.setattr(
        module,
        "_platform_user_payload",
        lambda *a, **kw: {"user_id": "authenticated-admin"},
    )
    instance = module.ConnectionHubEntrypoint.__new__(module.ConnectionHubEntrypoint)
    return SimpleNamespace(module=module, instance=instance, service=service)


def test_operations_are_declared_as_csrf_protected_posts() -> None:
    module = _entrypoint_module()
    methods = {
        alias: getattr(
            getattr(module.ConnectionHubEntrypoint, alias),
            "__bundle_api_method__",
        )
        for alias in (
            "project_person_control_get",
            "project_person_control_create",
            "project_person_control_update",
            "project_person_control_revoke",
            "project_person_my_card_seed",
        )
    }

    assert all(method.alias == alias for alias, method in methods.items())
    assert all(method.http_method == "POST" for method in methods.values())
    assert set(methods).issubset(module.CSRF_PROTECTED_OPERATION_ALIASES)
    authorize = getattr(
        module.ConnectionHubEntrypoint.project_operation_authorize,
        "__bundle_api_method__",
    )
    assert authorize.alias == "project_operation_authorize"
    assert authorize.http_method == "POST"
    assert authorize.csrf is False
    assert authorize.alias in module.CSRF_EXEMPT_POST_OPERATION_ALIASES


def test_template_names_project_membership_provider() -> None:
    bundle_root = Path(__file__).resolve().parents[1]
    template = yaml.safe_load(
        (bundle_root / "config" / "bundles.template.yaml").read_text(
            encoding="utf-8"
        )
    )
    config = template["bundles"]["items"][0]["config"]

    assert config["project_membership"] == {
        "provider": {
            "bundle_id": "problem-board@1-0",
            "operation": "project_membership_resolve",
        },
        "administrative_roles": ["owner", "admin"],
    }
    assert (
        config["surfaces"]["as_provider"]["api"]["operations"]
        ["project_operation_authorize"]
        ["visibility"]
        == {"user_types": [], "roles": []}
    )


@pytest.mark.asyncio
async def test_operations_use_authenticated_actor_and_host_request_id(entrypoint) -> None:
    request = SimpleNamespace(
        state=SimpleNamespace(request_id="host-request-7"),
        scope={},
    )
    forged = {
        "project_ref": "work:project:quickstart",
        "target_subject": "platform-user-2",
        "actor_subject": "forged-admin",
        "request_id": "forged-request",
    }

    results = []
    results.append(
        await entrypoint.module.ConnectionHubEntrypoint.project_person_control_get(
            entrypoint.instance,
            data=forged,
            request=request,
        )
    )
    results.append(
        await entrypoint.module.ConnectionHubEntrypoint.project_person_control_create(
            entrypoint.instance,
            data={**forged, "label": "Quickstart member", "migration": True},
            request=request,
        )
    )
    results.append(
        await entrypoint.module.ConnectionHubEntrypoint.project_person_control_update(
            entrypoint.instance,
            data={
                **forged,
                "label": "Narrowed member",
                "expected_card_revision": 1,
            },
            request=request,
        )
    )
    results.append(
        await entrypoint.module.ConnectionHubEntrypoint.project_person_control_revoke(
            entrypoint.instance,
            data=forged,
            request=request,
        )
    )
    results.append(
        await entrypoint.module.ConnectionHubEntrypoint.project_person_my_card_seed(
            entrypoint.instance,
            data={
                **forged,
                "resource_grants": {
                    "https://board.example.test/mcp": ["work:review"],
                },
                "resource_operations": {
                    "https://board.example.test/mcp": ["review.accept"],
                },
                "named_service_operations": {},
                "account_scope": {},
            },
            request=request,
        )
    )
    results.append(
        await entrypoint.module.ConnectionHubEntrypoint.project_operation_authorize(
            entrypoint.instance,
            data={
                **forged,
                "resource": "https://board.example.test/mcp",
                "operation": "review.accept",
                "required_grants": ["work:review"],
            },
            request=request,
        )
    )

    assert all(result.get("ok") is True for result in results)
    assert [call[0] for call in entrypoint.service.calls] == [
        "get",
        "create",
        "update",
        "revoke",
        "seed",
        "authorize",
    ]
    for operation, call in entrypoint.service.calls:
        assert call["user"] == {"user_id": "authenticated-admin"}
        assert call["project_ref"] == "work:project:quickstart"
        assert "actor_subject" not in call
        assert "person_subject" not in call
        if operation == "authorize":
            assert call["resource"] == "https://board.example.test/mcp"
            assert call["operation"] == "review.accept"
            assert call["required_grants"] == ["work:review"]
        else:
            assert call["target_subject"] == "platform-user-2"
            assert call["request_id"] == "host-request-7"
            if operation == "create":
                assert call["migration"] is True
            if operation == "seed":
                assert call["resource_grants"] == {
                    "https://board.example.test/mcp": ["work:review"]
                }
                assert call["resource_operations"] == {
                    "https://board.example.test/mcp": ["review.accept"]
                }


@pytest.mark.asyncio
async def test_project_authorization_port_accepts_async_host_factory() -> None:
    module = _entrypoint_module()
    port = object()

    async def _factory():
        return port

    entrypoint = SimpleNamespace(project_authorization_port_factory=_factory)

    assert await module._project_authorization_port(entrypoint) is port


@pytest.mark.asyncio
async def test_descriptor_port_calls_configured_membership_operation() -> None:
    module = _entrypoint_module()
    calls = []

    async def _call(**kwargs):
        calls.append(kwargs)
        subject = kwargs["data"]["subject"]
        return {
            "project_membership_resolve": {
                "ok": True,
                "membership": {
                    "project_ref": "work:project:quickstart",
                    "subject": subject,
                    "role": (
                        "owner" if subject == "authenticated-admin" else "member"
                    ),
                    "delegable_grants": ["work:admin", "work:review"],
                    "evidence": {"source": "problem-board"},
                },
            },
        }

    entrypoint = SimpleNamespace(
        bundle_props={
            "project_membership": {
                "provider": {
                    "bundle_id": "problem-board@1-0",
                    "operation": "project_membership_resolve",
                },
                "administrative_roles": ["owner", "admin"],
            }
        }
    )
    port = module.descriptor_project_authorization_port(
        entrypoint,
        caller=_call,
    )
    request = ProjectAuthorizationRequest.build(
        actor_subject="authenticated-admin",
        project_ref="work:project:quickstart",
        target_subject="platform-user-2",
        operation=PROJECT_PERSON_CONTROL_CREATE,
        request_id="request-1",
    )

    decision = await port.authorize_project_person_control(request)

    assert decision.allowed is True
    assert decision.delegable_grants == ("work:admin", "work:review")
    assert calls == [
        {
            "bundle_id": "problem-board@1-0",
            "operation": "project_membership_resolve",
            "data": {
                "project_ref": "work:project:quickstart",
                "subject": "authenticated-admin",
            },
        },
        {
            "bundle_id": "problem-board@1-0",
            "operation": "project_membership_resolve",
            "data": {
                "project_ref": "work:project:quickstart",
                "subject": "platform-user-2",
            },
        },
    ]


@pytest.mark.asyncio
async def test_descriptor_port_without_provider_refuses_by_name() -> None:
    module = _entrypoint_module()
    port = module.descriptor_project_authorization_port(
        SimpleNamespace(bundle_props={}),
    )
    request = ProjectAuthorizationRequest.build(
        actor_subject="authenticated-admin",
        project_ref="work:project:quickstart",
        target_subject="platform-user-2",
        operation=PROJECT_PERSON_CONTROL_CREATE,
        request_id="request-1",
    )

    decision = await port.authorize_project_person_control(request)

    assert decision.allowed is False
    assert decision.reason == "project_membership_resolver_missing"


@pytest.mark.asyncio
async def test_descriptor_port_reads_platform_result_and_structured_refusal() -> None:
    module = _entrypoint_module()
    responses = [
        {
            "status": "ok",
            "result": {
                "ok": True,
                "membership": {
                    "project_ref": "work:project:quickstart",
                    "subject": "authenticated-admin",
                    "role": "owner",
                    "delegable_grants": ["work:review"],
                },
            },
        },
        {
            "project_membership_resolve": {
                "ok": False,
                "error": {
                    "code": "work_identity_required",
                    "message": "A signed-in person is required.",
                },
            }
        },
    ]

    async def _call(**_kwargs):
        return responses.pop(0)

    port = module.descriptor_project_authorization_port(
        SimpleNamespace(
            bundle_props={
                "project_membership": {
                    "provider": {
                        "bundle_id": "problem-board@1-0",
                        "operation": "project_membership_resolve",
                    },
                    "administrative_roles": ["owner"],
                }
            }
        ),
        caller=_call,
    )
    decision = await port.authorize_project_person_control(
        ProjectAuthorizationRequest.build(
            actor_subject="authenticated-admin",
            project_ref="work:project:quickstart",
            target_subject="platform-user-2",
            operation=PROJECT_PERSON_CONTROL_CREATE,
            request_id="request-structured-refusal",
        )
    )

    assert decision.allowed is False
    assert decision.reason == "work_identity_required"


@pytest.mark.asyncio
async def test_descriptor_port_refuses_unknown_provider_envelope_by_name() -> None:
    module = _entrypoint_module()

    async def _call(**_kwargs):
        return {"status": "refused", "result": {"ok": True}}

    port = module.descriptor_project_authorization_port(
        SimpleNamespace(
            bundle_props={
                "project_membership": {
                    "provider": {
                        "bundle_id": "problem-board@1-0",
                        "operation": "project_membership_resolve",
                    },
                    "administrative_roles": ["owner"],
                }
            }
        ),
        caller=_call,
    )
    decision = await port.authorize_project_person_control(
        ProjectAuthorizationRequest.build(
            actor_subject="authenticated-admin",
            project_ref="work:project:quickstart",
            target_subject="platform-user-2",
            operation=PROJECT_PERSON_CONTROL_CREATE,
            request_id="request-unknown-envelope",
        )
    )

    assert decision.allowed is False
    assert decision.reason == "bundle_operation_result_shape_invalid"


@pytest.mark.asyncio
async def test_raising_port_factory_isolated_to_project_person_operations(
    monkeypatch,
) -> None:
    module = _entrypoint_module()

    class _EmptyCards:
        async def load_current(self, access_id, *, subject_hash):
            del access_id, subject_hash
            return None

    async def _factory():
        raise RuntimeError("policy host unavailable")

    async def _card_persistence(*_args):
        return _EmptyCards()

    async def _grant_store(_entrypoint):
        return object()

    monkeypatch.setattr(
        module,
        "_runtime_tenant_project",
        lambda _entrypoint: ("demo-tenant", "demo-project"),
    )
    monkeypatch.setattr(
        module,
        "_delegated_authority_config",
        lambda _entrypoint: SimpleNamespace(backend="redis-migration-source"),
    )
    monkeypatch.setattr(module, "_delegated_catalog_resolver", lambda *_args: object())
    monkeypatch.setattr(module, "_delegated_card_persistence", _card_persistence)
    monkeypatch.setattr(module, "_oauth_grant_store", _grant_store)
    monkeypatch.setattr(module, "_invocation_policy_service", lambda _entrypoint: None)

    service = await module._automation_access_service_for(
        SimpleNamespace(
            redis=object(),
            project_authorization_port_factory=_factory,
        ),
        SimpleNamespace(),
    )

    assert await service.revoke_access(
        {"user_id": "card-owner"},
        access_id="missing-card",
    ) == {"ok": True, "removed": False}
    assert await service.project_person_control_get(
        {"user_id": "project-admin"},
        project_ref="work:project:quickstart",
        target_subject="platform-user-2",
        request_id="request-port-failure",
    ) == {
        "ok": False,
        "error": "project_person_control_authorization_unavailable",
        "reason": "authorization_port_not_configured",
        "retryable": True,
        "status": 503,
    }


@pytest.mark.asyncio
async def test_project_authorization_runs_through_request_bound_bundle_operation(
    monkeypatch,
) -> None:
    module = _entrypoint_module()
    service = _Service()

    async def _access_service(*_args, **_kwargs):
        return service

    monkeypatch.setattr(module, "_automation_access_service", _access_service)
    monkeypatch.setattr(
        module,
        "_platform_user_payload",
        lambda *a, **kw: {"user_id": kw.get("user_id")},
    )
    connection_hub = module.ConnectionHubEntrypoint.__new__(
        module.ConnectionHubEntrypoint
    )

    class _ProblemBoardWorkflow:
        @api(method="POST", alias="authorize_review", route="operations")
        async def authorize_review(self, **kwargs):
            return await call_bundle_operation(
                bundle_id="connection-hub@1-0",
                operation="project_operation_authorize",
                data={
                    "project_ref": kwargs.get("project_ref"),
                    "resource": "https://board.example.test/mcp",
                    "operation": "review.accept",
                    "required_grants": ["work:review"],
                    "person_subject": "forged-subject",
                },
            )

    async def _load_bundle_workflow(**kwargs):
        if kwargs.get("bundle_id") == "connection-hub@1-0":
            return (
                connection_hub,
                SimpleNamespace(id="connection-hub@1-0"),
                "tenant-a",
                "project-a",
            )
        return (
            _ProblemBoardWorkflow(),
            SimpleNamespace(id="problem-board@1-0"),
            "tenant-a",
            "project-a",
        )

    monkeypatch.setattr(integrations, "_load_bundle_workflow", _load_bundle_workflow)
    monkeypatch.setattr(
        integrations,
        "_authoritative_bundle_props",
        lambda *args, **kwargs: {},
    )

    app = FastAPI()
    app.state.redis_async = object()
    app.state.pg_pool = None
    request = Request(
        {
            "type": "http",
            "app": app,
            "method": "POST",
            "path": (
                "/api/integrations/bundles/tenant-a/project-a/"
                "problem-board@1-0/operations/authorize_review"
            ),
            "query_string": b"",
            "headers": [],
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 12345),
            "http_version": "1.1",
        }
    )
    session = SimpleNamespace(
        session_id="session-1",
        user_type=SimpleNamespace(value="registered"),
        user_id="platform-user-7",
        fingerprint="fp-7",
        roles=["registered"],
        permissions=[],
        request_context=SimpleNamespace(),
    )

    result = await integrations._call_bundle_op_inner(
        tenant="tenant-a",
        project="project-a",
        bundle_id="problem-board@1-0",
        payload=integrations.BundleSuggestionsRequest(
            data={"project_ref": "work:project:quickstart"}
        ),
        request=request,
        operation="authorize_review",
        route="operations",
        session=session,
    )

    decision = result["authorize_review"]["project_operation_authorize"]
    assert decision == {
        "ok": True,
        "allowed": True,
        "reason": "project_operation_allowed",
    }
    operation, call = service.calls[-1]
    assert operation == "authorize"
    assert call["user"] == {"user_id": "platform-user-7"}
    assert "person_subject" not in call


@pytest.mark.asyncio
async def test_project_authorization_local_caller_returns_raw_evaluation_envelope(
    entrypoint,
    monkeypatch,
) -> None:
    request = SimpleNamespace(
        state=SimpleNamespace(request_id="local-request-1"),
        scope={},
    )

    async def _invoke_local(call, **_kwargs):
        return await entrypoint.module.ConnectionHubEntrypoint.project_operation_authorize(
            entrypoint.instance,
            data=call.data,
            request=request,
        )

    monkeypatch.setattr(
        bundle_operation_runtime,
        "invoke_local_bundle_operation",
        _invoke_local,
    )
    caller = make_local_bundle_operation_caller(
        redis=object(),
        pg_pool=None,
        comm_context=SimpleNamespace(),
    )

    result = await caller(
        BundleOperationCall(
            bundle_id="connection-hub@1-0",
            operation="project_operation_authorize",
            data={
                "project_ref": "work:project:quickstart",
                "resource": "https://board.example.test/mcp",
                "operation": "review.accept",
                "required_grants": ["work:review"],
            },
        )
    )

    assert result == {
        "ok": True,
        "allowed": True,
        "reason": "project_operation_allowed",
    }


@pytest.mark.asyncio
async def test_project_control_create_resolves_membership_through_bundle_operation(
    monkeypatch,
) -> None:
    module = _entrypoint_module()
    membership_calls = []
    provider_config = {
        "project_membership": {
            "provider": {
                "bundle_id": "problem-board@1-0",
                "operation": "project_membership_resolve",
            },
            "administrative_roles": ["owner", "admin"],
        }
    }

    class _CreateService:
        async def project_person_control_create(self, user, **kwargs):
            port = module.descriptor_project_authorization_port(
                SimpleNamespace(bundle_props=provider_config)
            )
            decision = await port.authorize_project_person_control(
                ProjectAuthorizationRequest.build(
                    actor_subject=user["user_id"],
                    project_ref=kwargs["project_ref"],
                    target_subject=kwargs["target_subject"],
                    operation=PROJECT_PERSON_CONTROL_CREATE,
                    request_id=kwargs["request_id"],
                )
            )
            return {
                "ok": decision.allowed,
                "reason": decision.reason,
                "delegable_grants": list(decision.delegable_grants),
            }

    async def _access_service(*_args, **_kwargs):
        return _CreateService()

    monkeypatch.setattr(module, "_automation_access_service", _access_service)
    monkeypatch.setattr(
        module,
        "_platform_user_payload",
        lambda *a, **kw: {"user_id": kw.get("user_id")},
    )
    connection_hub = module.ConnectionHubEntrypoint.__new__(
        module.ConnectionHubEntrypoint
    )

    class _ProblemBoardWorkflow:
        @api(
            method="POST",
            alias="project_membership_resolve",
            route="operations",
        )
        async def project_membership_resolve(self, **kwargs):
            subject = kwargs.get("subject")
            membership_calls.append(subject)
            return {
                "ok": True,
                "membership": {
                    "project_ref": kwargs.get("project_ref"),
                    "subject": subject,
                    "role": "owner" if subject == "platform-admin-1" else "member",
                    "delegable_grants": ["work:review"],
                    "evidence": {"source": "problem-board"},
                },
            }

    async def _load_bundle_workflow(**kwargs):
        if kwargs.get("bundle_id") == "connection-hub@1-0":
            return (
                connection_hub,
                SimpleNamespace(id="connection-hub@1-0"),
                "tenant-a",
                "project-a",
            )
        return (
            _ProblemBoardWorkflow(),
            SimpleNamespace(id="problem-board@1-0"),
            "tenant-a",
            "project-a",
        )

    monkeypatch.setattr(integrations, "_load_bundle_workflow", _load_bundle_workflow)
    monkeypatch.setattr(
        integrations,
        "_authoritative_bundle_props",
        lambda *args, **kwargs: {},
    )

    app = FastAPI()
    app.state.redis_async = object()
    app.state.pg_pool = None
    request = Request(
        {
            "type": "http",
            "app": app,
            "method": "POST",
            "path": (
                "/api/integrations/bundles/tenant-a/project-a/"
                "connection-hub@1-0/operations/project_person_control_create"
            ),
            "query_string": b"",
            "headers": [],
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 12345),
            "http_version": "1.1",
        }
    )
    session = SimpleNamespace(
        session_id="session-admin",
        user_type=SimpleNamespace(value="registered"),
        user_id="platform-admin-1",
        fingerprint="fp-admin",
        roles=["registered"],
        permissions=[],
        request_context=SimpleNamespace(),
    )

    result = await integrations._call_bundle_op_inner(
        tenant="tenant-a",
        project="project-a",
        bundle_id="connection-hub@1-0",
        payload=integrations.BundleSuggestionsRequest(
            data={
                "project_ref": "work:project:quickstart",
                "target_subject": "platform-user-2",
            }
        ),
        request=request,
        operation="project_person_control_create",
        route="operations",
        session=session,
    )

    assert result["project_person_control_create"] == {
        "ok": True,
        "reason": "",
        "delegable_grants": ["work:review"],
    }
    assert membership_calls == ["platform-admin-1", "platform-user-2"]
