from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from connection_hub.delegated_credentials.request_approval import (
    RequestApprovalTicket,
    issue_request_approval_ticket,
)
from connection_hub.invocation_policy import (
    POLICY_CHANGE_COMMITTED,
    POLICY_ONCE,
    SURFACE_OUTER,
    InvocationAuthority,
    InvocationPolicyConflict,
    InvocationPolicyRecordError,
)
from kdcube_ai_app.apps.chat.sdk.runtime.dynamic_module_loader import (
    load_dynamic_module_for_path,
)


SECRET = "request-approval-secret-with-at-least-thirty-two-bytes"
RESOURCE = "urn:kdcube:management:deployment:tenant-a:project-a"
OPERATION = "kdcube.management.application.reload"
DIGEST = "a" * 64


def _entrypoint_module():
    bundle_root = Path(__file__).resolve().parents[1]
    _name, module = load_dynamic_module_for_path(bundle_root / "entrypoint.py")
    return module


def _connections() -> dict:
    return {
        "delegated_credentials": {
            "admission": {
                "enabled": True,
                "services": {
                    "kdcube-management": {
                        "secret_ref": "admission.kdcube-management.secret",
                        "resources": [
                            "urn:kdcube:management:deployment:*:*"
                        ],
                        "request_bound_operations": [OPERATION],
                        "request_permit_ttl_seconds": 600,
                    }
                },
            }
        }
    }


def _ticket(**overrides) -> str:
    now = int(time.time())
    values = {
        "service_id": "kdcube-management",
        "client_id": "connection-hub-cli",
        "access_id": "access-1",
        "resource": RESOURCE,
        "operation": OPERATION,
        "invocation_id": "invocation-1",
        "request_digest": DIGEST,
        "card_revision": 7,
        "authority_revision": "catalog-9",
        "issued_at": now,
        "expires_at": now + 600,
        "approval_context": {"application_id": "workspace@1-0"},
    }
    values.update(overrides)
    return issue_request_approval_ticket(
        RequestApprovalTicket(**values),
        secret=SECRET,
    )


class _AccessService:
    def __init__(self, catalog_version: str = "catalog-9") -> None:
        self.catalog_version = catalog_version

    async def active_catalog_version(self) -> str:
        return self.catalog_version


class _PolicyService:
    def __init__(self, change=None) -> None:
        self.change = change

    async def get_policy_change(self, **_kwargs):
        return self.change


def _authority() -> InvocationAuthority:
    return InvocationAuthority(
        access_id="access-1",
        resource=RESOURCE,
        surface=SURFACE_OUTER,
        operation=OPERATION,
    )


async def _validate(module, *, ticket: str, **overrides):
    values = {
        "access_service": _AccessService(),
        "invocation_policy_service": _PolicyService(),
        "owner_subject": "user-1",
        "authority": _authority(),
        "approval_ticket": ticket,
        "client_id": "connection-hub-cli",
        "access_id": "access-1",
        "resource": RESOURCE,
        "operation": OPERATION,
        "invocation_id": "invocation-1",
        "request_digest": DIGEST,
        "request_card_revision": 7,
        "request_authority_revision": "catalog-9",
        "approval_context": {"application_id": "workspace@1-0"},
        "invocation_mode": POLICY_ONCE,
        "card": {"card_revision": 7},
    }
    values.update(overrides)
    return await module._validated_request_approval(object(), **values)


@pytest.fixture()
def module(monkeypatch):
    loaded = _entrypoint_module()
    monkeypatch.setattr(loaded, "_connections_config", lambda _entrypoint: _connections())

    async def _secret(*_args, **_kwargs):
        return SECRET

    monkeypatch.setattr(loaded, "_bundle_secret_value", _secret)
    return loaded


@pytest.mark.asyncio
async def test_request_bound_browser_handoff_accepts_exact_signed_display(module):
    verified = await _validate(module, ticket=_ticket())

    assert verified.invocation_id == "invocation-1"
    assert verified.approval_context == {"application_id": "workspace@1-0"}


@pytest.mark.asyncio
async def test_request_bound_browser_handoff_rejects_signature_tampering(module):
    token = _ticket()
    tampered = f"{token[:-1]}{'0' if token[-1] != '0' else '1'}"

    with pytest.raises(InvocationPolicyRecordError) as raised:
        await _validate(module, ticket=tampered)

    assert raised.value.reason == "request_approval_signature_invalid"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed",
    [
        {"invocation_id": "another-invocation"},
        {"request_digest": "b" * 64},
        {"request_card_revision": 8},
        {"request_authority_revision": "catalog-10"},
        {"approval_context": {"application_id": "another-app@1-0"}},
    ],
)
async def test_request_bound_browser_handoff_rejects_changed_url_fields(
    module,
    changed,
):
    with pytest.raises(InvocationPolicyConflict) as raised:
        await _validate(module, ticket=_ticket(), **changed)

    assert raised.value.reason in {
        "request_approval_identity_moved",
        "request_approval_display_moved",
    }


@pytest.mark.asyncio
async def test_request_bound_browser_handoff_rejects_live_authority_drift(module):
    with pytest.raises(InvocationPolicyConflict) as raised:
        await _validate(
            module,
            ticket=_ticket(),
            access_service=_AccessService("catalog-10"),
        )

    assert raised.value.reason == "request_approval_authority_moved"


@pytest.mark.asyncio
async def test_exact_committed_replay_reuses_ticket_after_card_revision_moves(module):
    change = SimpleNamespace(
        state=POLICY_CHANGE_COMMITTED,
        change_id="invocation-1",
        mode=POLICY_ONCE,
    )

    verified = await _validate(
        module,
        ticket=_ticket(),
        invocation_policy_service=_PolicyService(change),
        access_service=_AccessService("catalog-10"),
        card={"card_revision": 8},
    )

    assert verified.card_revision == 7


@pytest.mark.asyncio
async def test_request_bound_browser_handoff_rejects_excessive_lifetime(module):
    now = int(time.time())
    with pytest.raises(InvocationPolicyRecordError) as raised:
        await _validate(
            module,
            ticket=_ticket(issued_at=now, expires_at=now + 601),
        )

    assert raised.value.reason == "request_approval_lifetime_invalid"
