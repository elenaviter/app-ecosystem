"""Focused consent must keep held namespace grants while committing its policy.

The route, authority resolver, in-memory Card persistence and file policy store
are real; host wiring and passive conversation notifications are substituted.
"""

from pathlib import Path

import pytest

from connection_hub.invocation_policy import POLICY_ALWAYS, POLICY_ONCE, SURFACE_OUTER, InvocationAuthority
from kdcube_ai_app.apps.chat.sdk.runtime.dynamic_module_loader import load_dynamic_module_for_path


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [POLICY_ONCE, POLICY_ALWAYS])
async def test_external_pending_grant_preserves_named_operations_with_its_policy(monkeypatch, tmp_path, mode):
    package_tests = Path(__file__).resolve().parents[3] / "packages/connection-hub/tests"
    monkeypatch.syspath_prepend(str(package_tests))
    from test_oauth_card_extension import (
        CLIENT, NAMED, UPLOAD, USER, _Harness, _assert_extended, _named_card, _named_connections,
    )

    harness = _Harness(tmp_path, connections=_named_connections())
    record = await _named_card(harness)
    _, module = load_dynamic_module_for_path(Path(__file__).resolve().parents[1] / "entrypoint.py")

    async def access_service(*_args, **_kwargs):
        return harness.service

    async def events(*_args, **_kwargs):
        return None

    monkeypatch.setattr(module, "_automation_access_service", access_service)
    monkeypatch.setattr(module, "_invocation_policy_service", lambda *_args: harness.policies)
    monkeypatch.setattr(module, "_platform_user_payload", lambda *_args, **_kwargs: {**USER, "sub": USER["user_id"]})
    monkeypatch.setattr(module.ConnectionHubEntrypoint, "_author_agent_grant_events", events)
    instance = module.ConnectionHubEntrypoint.__new__(module.ConnectionHubEntrypoint)
    instance.redis = object()
    payload = {
        "client_id": CLIENT, "access_id": record.access_id, "resource": NAMED,
        "claims": ["named_services:use", "mail:send"],
        "resource_operations": {NAMED: ["named_services_call"]},
        "named_service_operations": {"mail": [UPLOAD]},
        "invocation_mode": mode, "invocation_change_id": "approve-mail-upload",
    }

    result = await instance.delegated_agent_grant_create(data=payload)

    assert result["ok"] is True, result
    assert result["invocation_policy"]["mode"] == mode
    _assert_extended(await harness.card(record.access_id))
    policy = await harness.policies.get(owner_subject=USER["user_id"], authority=InvocationAuthority(
        access_id=record.access_id, resource=NAMED, surface=SURFACE_OUTER, operation="named_services_call",
    ))
    assert policy is not None and policy.mode == mode
    # Retry replays the same committed policy and Card, without another revision.
    replay = await instance.delegated_agent_grant_create(data=payload)
    assert replay["ok"] is True and replay["replay"] is True, replay
    assert (await harness.card(record.access_id)).card_revision == record.card_revision + 1
    _assert_extended(await harness.card(record.access_id))
