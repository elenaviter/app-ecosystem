"""W502 join: the pending-invitation revision read is a session read the board asks, never a browser form."""
from __future__ import annotations

from pathlib import Path

from kdcube_ai_app.apps.chat.sdk.runtime.dynamic_module_loader import load_dynamic_module_for_path


def test_the_read_is_exposed_csrf_exempt_and_not_protected():
    _name, module = load_dynamic_module_for_path(Path(__file__).resolve().parents[1] / "entrypoint.py")
    assert "project_invitation_pending_revision" in module.CSRF_EXEMPT_POST_OPERATION_ALIASES
    assert "project_invitation_pending_revision" not in module.CSRF_PROTECTED_OPERATION_ALIASES
    assert callable(getattr(module.ConnectionHubEntrypoint, "project_invitation_pending_revision"))
