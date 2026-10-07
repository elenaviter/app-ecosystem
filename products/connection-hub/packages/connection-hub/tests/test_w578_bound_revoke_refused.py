"""W578: with Card transactions on, a Card bound under a Control is not ended by a direct revoke.

A person's My Card or a project-bound agent Card carries authority its
Control's owner relies on (for a project, its usable administrators), so only
that owner's transaction may end it. Until that is enlisted the revoke refuses
finitely, before anything is ended; an unbound Card and a disabled Hub are
unchanged.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from connection_hub.delegated_credentials import automation_access as module
from connection_hub.delegated_credentials.automation_access import AutomationAccessService

USER = {"user_id": "platform-user-1"}


class _PastTheGuard(Exception):
    pass


def _service(*, enabled: bool, bound: bool) -> AutomationAccessService:
    service = AutomationAccessService.__new__(AutomationAccessService)
    record = SimpleNamespace(grantor_subject="platform-user-1", control_card=object() if bound else None)

    async def load(access_id, *, grantor_subject):
        return record, module.CARD_STATE_ACTIVE

    def issuer_managed(_record):
        raise _PastTheGuard()

    service._load_record_any_state = load
    service._issuer_managed = issuer_managed
    if enabled:
        service.bind_card_coordinator(object(), intents=object(), decisions=object())
    return service


@pytest.mark.asyncio
async def test_enabled_a_bound_card_revoke_refuses_before_anything_is_ended():
    result = await _service(enabled=True, bound=True).revoke_access(USER, access_id="card-1")
    assert result["error"] == "card_transactions_direct_write_refused" and result["status"] == 409
    assert result["retryable"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled,bound", [(True, False), (False, True), (False, False)])
async def test_an_unbound_card_or_a_disabled_hub_revokes_as_before(enabled, bound):
    with pytest.raises(_PastTheGuard):
        await _service(enabled=enabled, bound=bound).revoke_access(USER, access_id="card-1")
