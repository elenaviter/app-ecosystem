"""W562: a project's Control Card is changed only through the project (W502).

The operator's scope, as quoted in
``test_a_control_card_not_held_by_a_project_stays_editable_by_its_creator``:
"only Control Cards that belong to a Problem Board project are edited through
the project". A project's Control Card is created with ``issuer_kind="project"``
and stored under its creator. These tests ask whether the creator's plain
Connection Hub path (``control_card_update`` / ``control_card_revoke``) still
changes or revokes it with no project question, and keep the creator's plain
path working for a Control Card that no project holds.

Every verdict reads the stored record in the Card persistence, not only the
call's ``ok``. The shared ``_Persistence`` fake has no ``forget`` (revoke);
this file gives its own instance one, here only.
"""

from __future__ import annotations

import asyncio
import dataclasses
from types import MethodType

import pytest

from connection_hub.delegated_credentials.cards.model import CARD_STATE_ACTIVE, CARD_STATE_REVOKED
from test_agent_capability_control_sync import NAMED_RESOURCE, _service, subject_hash_for

PROJECT = "work:project:one"
CREATOR = "owner-one"


async def _forget(self, authority, *, subject_hash, revoked_authority=None):
    """Test-local revoke for this file's persistence instance: the record turns revoked."""

    held = self.records.get(authority.access_id)
    if held is None or subject_hash_for(held[0].grantor_subject) != subject_hash:
        raise AssertionError("forget of a record this owner does not hold")
    revoked = revoked_authority or dataclasses.replace(held[0], state=CARD_STATE_REVOKED)
    self.records[authority.access_id] = (revoked, held[1])
    self.persisted.append(f"forget:{authority.access_id}")


def _card(issuer_kind: str, issuer_ref: str):
    service, persistence = _service(named_services=True)
    persistence.forget = MethodType(_forget, persistence)
    creator = {"user_id": CREATOR, "roles": ["kdcube:role:super-admin"], "permissions": []}
    made = asyncio.run(service.control_card_create(
        creator, issuer_ref=issuer_ref, issuer_kind=issuer_kind, issuer_label="One",
    ))
    assert made["ok"] is True, made
    return service, persistence, creator, made["control_card"]["access_id"]


def _stored(persistence, control_id):
    return persistence.records[control_id][0]


@pytest.mark.parametrize("kind,ref", [("project", PROJECT)])
def test_the_creator_cannot_widen_a_projects_control_card_on_the_plain_path(kind, ref):
    service, persistence, creator, control_id = _card(kind, ref)
    before = _stored(persistence, control_id)
    assert before.issuer_kind == "project" and before.state == CARD_STATE_ACTIVE

    changed = asyncio.run(service.control_card_update(
        creator, control_id=control_id, resource_grants={NAMED_RESOURCE: ["named_services:use", "slack:read"]},
    ))

    after = _stored(persistence, control_id)
    stored_changed = after != before
    assert not stored_changed and changed.get("ok") is not True, (
        f"plain path on a project's Control Card: ok={changed.get('ok')}, "
        f"stored revision {before.card_revision}->{after.card_revision}, stored changed={stored_changed}"
    )


def test_the_creator_cannot_revoke_a_projects_control_card_on_the_plain_path():
    service, persistence, creator, control_id = _card("project", PROJECT)
    before = _stored(persistence, control_id)

    revoked = asyncio.run(service.control_card_revoke(creator, control_id=control_id))

    after = _stored(persistence, control_id)
    assert after.state == CARD_STATE_ACTIVE and revoked.get("ok") is not True, (
        f"plain path revoke on a project's Control Card: ok={revoked.get('ok')}, "
        f"stored state {before.state}->{after.state}"
    )


def test_positive_control_the_creator_edits_a_control_card_no_project_holds():
    service, persistence, creator, control_id = _card("application", "agent:resident:helper")
    before = _stored(persistence, control_id)

    changed = asyncio.run(service.control_card_update(
        creator, control_id=control_id, resource_grants={NAMED_RESOURCE: ["named_services:use", "slack:read"]},
    ))

    after = _stored(persistence, control_id)
    assert changed.get("ok") is True, changed
    assert after.card_revision == before.card_revision + 1 and after != before


def test_positive_control_the_creator_revokes_a_control_card_no_project_holds():
    service, persistence, creator, control_id = _card("application", "agent:resident:helper")

    revoked = asyncio.run(service.control_card_revoke(creator, control_id=control_id))

    assert revoked.get("ok") is True, revoked
    assert _stored(persistence, control_id).state == CARD_STATE_REVOKED
