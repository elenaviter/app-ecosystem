"""W562: a project's Control Card is changed only through the project (W502).

The operator's scope, as quoted in
``test_a_control_card_not_held_by_a_project_stays_editable_by_its_creator``:
"only Control Cards that belong to a Problem Board project are edited through
the project". A project's Control Card is created with ``issuer_kind="project"``
and stored under its creator. These tests ask whether the creator's plain
Connection Hub path still changes or revokes it with no project question.
"""

from __future__ import annotations

import asyncio

from test_project_control_card_access import _real_card


def test_the_creator_cannot_widen_a_projects_control_card_on_the_plain_path():
    service, creator, control_id, resource = _real_card()
    before = asyncio.run(service.control_card_get(creator, control_id=control_id))["access"]
    assert before["issuer_kind"] == "project"

    changed = asyncio.run(service.control_card_update(
        creator, control_id=control_id, resource_grants={resource: ["named_services:use", "slack:read"]},
    ))

    assert changed.get("ok") is not True, "the plain path changed a project's Control Card"
    after = asyncio.run(service.control_card_get(creator, control_id=control_id))["access"]
    assert after.get("resource_grants") == before.get("resource_grants")


# control_card_revoke on the plain path is not exercised here: this harness's
# persistence fake has no `forget` (AttributeError at automation_access.py:2288),
# so a revoke test would fail for the fixture, not for the route.
