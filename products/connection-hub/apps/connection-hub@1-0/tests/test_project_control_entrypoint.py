"""Connection Hub exposes the project-control Card boundary to peer apps."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from kdcube_ai_app.apps.chat.sdk.runtime.dynamic_module_loader import (
    load_dynamic_module_for_path,
)


def _entrypoint_module():
    bundle_root = Path(__file__).resolve().parents[1]
    _name, module = load_dynamic_module_for_path(bundle_root / "entrypoint.py")
    return module


class _Service:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def project_control_basis(self, user, **kwargs):
        self.calls.append(("basis", {"user": user, **kwargs}))
        return {"ok": True}

    async def attach_project_control(self, user, **kwargs):
        self.calls.append(("attach", {"user": user, **kwargs}))
        return {"ok": True}

    async def detach_project_control(self, user, **kwargs):
        self.calls.append(("detach", {"user": user, **kwargs}))
        return {"ok": True}


@pytest.fixture()
def entrypoint(monkeypatch):
    module = _entrypoint_module()
    service = _Service()
    monkeypatch.setattr(module, "_automation_access_service", lambda *a, **kw: service)
    monkeypatch.setattr(
        module,
        "_platform_user_payload",
        lambda *a, **kw: {"user_id": "platform-user-1"},
    )
    instance = module.ConnectionHubEntrypoint.__new__(module.ConnectionHubEntrypoint)
    return SimpleNamespace(module=module, instance=instance, service=service)


def test_project_control_operations_are_declared_with_expected_csrf_boundary() -> None:
    module = _entrypoint_module()
    basis = getattr(
        module.ConnectionHubEntrypoint.delegated_access_project_control_basis,
        "__bundle_api_method__",
    )
    attach = getattr(
        module.ConnectionHubEntrypoint.delegated_access_project_control_attach,
        "__bundle_api_method__",
    )
    detach = getattr(
        module.ConnectionHubEntrypoint.delegated_access_project_control_detach,
        "__bundle_api_method__",
    )

    assert basis.alias == "delegated_access_project_control_basis"
    assert attach.alias == "delegated_access_project_control_attach"
    assert detach.alias == "delegated_access_project_control_detach"
    assert basis.http_method == attach.http_method == detach.http_method == "POST"
    assert {
        "delegated_access_project_control_basis",
        "delegated_access_project_control_attach",
        "delegated_access_project_control_detach",
    }.issubset(module.CSRF_PROTECTED_OPERATION_ALIASES)


@pytest.mark.asyncio
async def test_project_control_operations_forward_identity_and_preconditions(entrypoint) -> None:
    await entrypoint.module.ConnectionHubEntrypoint.delegated_access_project_control_basis(
        entrypoint.instance,
        data={"access_id": "agent-card-1"},
    )
    await entrypoint.module.ConnectionHubEntrypoint.delegated_access_project_control_attach(
        entrypoint.instance,
        data={
            "access_id": "agent-card-1",
            "control_id": "project-control-1",
            "expected_card_revision": 7,
        },
    )
    await entrypoint.module.ConnectionHubEntrypoint.delegated_access_project_control_detach(
        entrypoint.instance,
        data={
            "access_id": "agent-card-1",
            "control_id": "project-control-1",
            "expected_card_revision": 8,
        },
    )

    assert entrypoint.service.calls == [
        (
            "basis",
            {
                "user": {"user_id": "platform-user-1"},
                "access_id": "agent-card-1",
            },
        ),
        (
            "attach",
            {
                "user": {"user_id": "platform-user-1"},
                "access_id": "agent-card-1",
                "control_id": "project-control-1",
                "expected_card_revision": 7,
            },
        ),
        (
            "detach",
            {
                "user": {"user_id": "platform-user-1"},
                "access_id": "agent-card-1",
                "control_id": "project-control-1",
                "expected_card_revision": 8,
            },
        ),
    ]


def test_card_editor_keeps_revoke_next_to_save_and_cancel() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "ui/widgets/connections/src/features/delegatedAccess/DelegatedAccessPanel.tsx"
    ).read_text(encoding="utf-8")
    start = source.index('<div className="form-actions form-actions--sticky">')
    sticky = source[start : source.index("</section>", start)]

    assert sticky.index("Save") < sticky.index("Cancel")
    assert sticky.index("Cancel") < sticky.index("renderRevokeControl(record)")

    agent_start = source.index("const renderDetailedAgentCard")
    agent_end = source.index("const renderDetailedOtherCard", agent_start)
    agent_editor = source[agent_start:agent_end]
    assert agent_editor.index("Save") < agent_editor.index("Cancel")
    assert agent_editor.index("Cancel") < agent_editor.index("renderRevokeControl(item)")

    client_start = agent_end
    client_end = source.index("const grantedPane", client_start)
    client_editor = source[client_start:client_end]
    assert client_editor.index("Save") < client_editor.index("Cancel")
    assert client_editor.index("Cancel") < client_editor.index("renderRevokeControl(item)")


def test_controlled_card_can_show_original_and_effective_authority() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "ui/widgets/connections/src/features/delegatedAccess/DelegatedAccessPanel.tsx"
    ).read_text(encoding="utf-8")

    assert "Project control in force" in source
    assert "Calls covered by this control remain closed." in source
    assert "Open project control" in source
    assert "Authority evidence" in source
    assert "Current deployment" in source
    assert ">\n              Original\n" in source
    assert ">\n              Effective\n" in source
    assert "const authority = displayedAuthority(item);" in source
