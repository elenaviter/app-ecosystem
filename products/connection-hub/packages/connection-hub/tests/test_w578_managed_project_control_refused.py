"""W578: with Card transactions enabled, a managed project's Control Card (P) takes no direct write.

P is created, changed, attached and revoked only through its project's
lifecycle plan and group transaction (CodeApp, 7 October 2026 03:18 UTC). P is
classified from the stored Card (an application Control at the id derived from
its project and holder) in a scope a configured caller plans
(``plan_scope_prefix``), never from a request label. Every native writer, the
project alias that reaches it, update_access/revoke_access and attach/detach
refuse ``card_transactions_direct_write_refused`` before any write: P stays
byte-identical. An application Control outside a managed scope, and every
writer with transactions disabled, are unchanged. A pure read stays a read.
"""

from __future__ import annotations

import pytest

from connection_hub.delegated_credentials.cards.store import subject_hash_for
from test_w502_person_control_binding import CREATOR, PROJECT_REF, _project_control, _service
from test_w580_bound_card_writers import _memories, redis_client  # noqa: F401 - fixture
from test_w502_my_card_fence_real_path import GRANT, OPERATION

REFUSED = "card_transactions_direct_write_refused"
OWNER = {"user_id": CREATOR}


def _enable(h, scopes=("work:project:",)):
    h.service.bind_card_coordinator(object(), intents=object(), decisions=object())
    h.service.bind_managed_control_scopes(scopes)


async def _stored(h, access_id, holder=CREATOR):
    return await h.store.read_current_authority(subject_hash=subject_hash_for(holder), access_id=access_id)


async def _other_control(h, issuer_ref="agent:resident:helper"):
    made = await h.service.control_card_create(OWNER, issuer_ref=issuer_ref, issuer_kind="application")
    assert made["ok"] is True, made
    return made["control_card"]["access_id"]


@pytest.mark.asyncio
async def test_every_direct_writer_of_a_managed_p_refuses_and_p_is_unchanged(tmp_path, redis_client):  # noqa: F811
    h = await _service(tmp_path, redis_client)
    p_id = await _project_control(h)
    other = await _other_control(h)
    _enable(h)
    before = await _stored(h, p_id)
    writes = {
        "control_card_create": lambda: h.service.control_card_create(
            OWNER, issuer_ref=PROJECT_REF, issuer_kind="application", initial_profile="read"),
        "control_card_update": lambda: h.service.control_card_update(
            OWNER, control_id=p_id, resource_grants={}, resource_operations={}),
        "update_access": lambda: h.service.update_access(OWNER, access_id=p_id, resource_grants={},
                                                         label="renamed"),
        "control_card_revoke": lambda: h.service.control_card_revoke(OWNER, control_id=p_id),
        "revoke_access": lambda: h.service.revoke_access(OWNER, access_id=p_id),
        "attach a parent above P": lambda: h.service.attach_control_card(OWNER, access_id=p_id, control_id=other),
        "attach P under a Card": lambda: h.service.attach_control_card(OWNER, access_id=other, control_id=p_id),
    }
    for name, write in writes.items():
        result = await write()
        assert result.get("error") == REFUSED and result.get("status") == 409, (name, result)
    assert await _stored(h, p_id) == before
    assert (await h.service._load_record(other, grantor_subject=CREATOR)).control_card is None
    read = await h.service.control_card_get(OWNER, control_id=p_id)
    assert read["ok"] is True, read


@pytest.mark.asyncio
async def test_a_link_to_a_managed_p_is_neither_replaced_nor_detached(tmp_path, redis_client):  # noqa: F811
    h = await _service(tmp_path, redis_client)
    p_id = await _project_control(h)
    child = await _other_control(h)
    replacement = await _other_control(h, issuer_ref="agent:resident:other")
    attached = await h.service.attach_control_card(OWNER, access_id=child, control_id=p_id)
    assert attached["ok"] is True, attached
    _enable(h)
    linked = await _stored(h, child)
    detached = await h.service.detach_control_card(OWNER, access_id=child, control_id=p_id)
    replaced = await h.service.attach_control_card(OWNER, access_id=child, control_id=replacement,
                                                   replace_control_id=p_id)
    assert detached["error"] == REFUSED and replaced["error"] == REFUSED, (detached, replaced)
    assert await _stored(h, child) == linked


@pytest.mark.asyncio
async def test_a_control_outside_a_managed_scope_and_disabled_mode_are_unchanged(tmp_path, redis_client):  # noqa: F811
    h = await _service(tmp_path, redis_client)
    p_id = await _project_control(h, grants=False)
    stored_p = await h.service._load_record(p_id, grantor_subject=CREATOR)
    other = await h.service._load_record(await _other_control(h), grantor_subject=CREATOR)
    _enable(h, scopes=("work:elsewhere:",))  # enabled, but this project's scope is not a planned one
    assert h.service._managed_project_control_refused(stored_p) is None
    _enable(h)
    assert h.service._managed_project_control_refused(stored_p)["error"] == REFUSED
    assert h.service._managed_project_control_refused(other) is None  # an unrelated application Control

    h2 = await _service(tmp_path / "disabled", redis_client)
    p2 = await _project_control(h2, grants=False)
    h2.service.bind_managed_control_scopes(("work:project:",))  # scopes alone, transactions off
    updated = await h2.service.control_card_update(
        OWNER, control_id=p2, resource_grants={_memories(): [GRANT]},
        resource_operations={_memories(): [OPERATION]}, _delegable_grants=(GRANT,))
    assert updated["ok"] is True, updated
    assert (await h2.service.control_card_revoke(OWNER, control_id=p2))["ok"] is True


def test_classification_is_the_planners_p_identity_not_a_label():
    from connection_hub.delegated_credentials.automation_access import AutomationAccessService
    from connection_hub.delegated_credentials.controls.model import control_card_id_for_issuer

    service = AutomationAccessService.__new__(AutomationAccessService)
    service.bind_card_coordinator(object(), intents=object(), decisions=object())
    service.bind_managed_control_scopes(("work:project:", "", None))
    p_id = control_card_id_for_issuer("application", PROJECT_REF, grantor_subject=CREATOR)
    managed = dict(access_id=p_id, issuer_kind="application", issuer_ref=PROJECT_REF, grantor_subject=CREATOR)
    assert service._managed_project_control(**managed)
    assert not service._managed_project_control(**{**managed, "access_id": "not-the-derived-id"})
    assert not service._managed_project_control(**{**managed, "issuer_kind": "project"})
    assert not service._managed_project_control(**{**managed, "grantor_subject": "someone-else"})
