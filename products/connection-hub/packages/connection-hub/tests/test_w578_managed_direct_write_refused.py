"""W578: with Card transactions enabled, no managed project Card write takes a direct path.

A project's person Control, its pending invitation, the bindings and the
person's My Card change together with the project host's membership, so the
host's transaction writes them (the Hub takes part through its participant).
Each direct writer refuses finitely before it reads or writes anything; with
transactions disabled nothing changes.
"""

from __future__ import annotations

import pytest

from connection_hub.delegated_credentials.automation_access import AutomationAccessService

USER = {"user_id": "platform-user-1"}
WRITERS = {
    "project_person_control_create": {"project_ref": "work:project:one", "target_subject": "user:a",
                                      "request_id": "r-1"},
    "project_person_control_update": {"project_ref": "work:project:one", "target_subject": "user:a",
                                      "request_id": "r-1"},
    "project_person_control_revoke": {"project_ref": "work:project:one", "target_subject": "user:a",
                                      "request_id": "r-1"},
    "project_person_control_bind_project": {"project_ref": "work:project:one", "target_subject": "user:a",
                                            "request_id": "r-1"},
    "project_person_control_bind_invitation": {"project_ref": "work:project:one", "invitation_ref": "inv-1",
                                               "control_id": "ctl-1", "request_id": "r-1"},
    "project_person_my_card_seed": {"project_ref": "work:project:one", "target_subject": "user:a",
                                    "resource_grants": {}, "resource_operations": {}, "request_id": "r-1"},
}


class _Untouchable:
    def __getattr__(self, name):
        raise AssertionError(f"a refused write reached {name}")


def _service(*, enabled: bool) -> AutomationAccessService:
    service = AutomationAccessService.__new__(AutomationAccessService)
    service._project_person_controls = _Untouchable()
    service._project_invitation_controls = _Untouchable()
    if enabled:
        service.bind_card_coordinator(object(), intents=object(), decisions=object())
    return service


def test_disabled_changes_nothing():
    assert _service(enabled=False)._managed_direct_write_refused() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("writer", sorted(WRITERS))
async def test_enabled_every_managed_direct_writer_refuses_before_anything_else(writer):
    result = await getattr(_service(enabled=True), writer)(USER, **WRITERS[writer])
    assert result == {"ok": False, "error": "card_transactions_direct_write_refused",
                      "message": "This project Card change is made through the project's own transaction.",
                      "retryable": False, "status": 409}


@pytest.mark.asyncio
async def test_enabled_a_refused_pending_invitation_write_reaches_nothing_either():
    service = _service(enabled=True)
    for writer in ("project_person_control_create", "project_person_control_update",
                   "project_person_control_revoke"):
        result = await getattr(service, writer)(USER, project_ref="work:project:one", invitation_ref="inv-1",
                                                request_id="r-1")
        assert result["error"] == "card_transactions_direct_write_refused"
