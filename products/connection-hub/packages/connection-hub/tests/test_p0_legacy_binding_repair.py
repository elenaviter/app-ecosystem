"""P0, 10 Oct 2026: a legacy person Control (issued before W502 bound C under P) and a My Card with an older
holder are bound at bundle load exactly as new Cards are; the Card-edit PLAN check then passes; a second
load changes nothing.

Live 11:07Z every Control Card save was refused card_plan_update_scope_invalid by
``existing_card_selection_plan._target_identity`` (kept strict). Live probe 11:2xZ: all 7 person Controls
unbound, 4 My Cards with an older holder, one P per project.
"""
from __future__ import annotations

import dataclasses
import time

import pytest
from service_foundation.coordination.durable_decision_log import DecisionRefused

from connection_hub.delegated_credentials.cards.model import ControlCardBinding
from connection_hub.delegated_credentials.cards.store import subject_hash_for
from connection_hub.delegated_credentials.existing_card_selection_plan import _target_identity
from connection_hub.delegated_credentials.legacy_binding_repair import repair_legacy_project_bindings
from connection_hub.delegated_credentials.project_identity_lifecycle import ProjectPersonCardIdentity

from test_project_person_access import PROJECT_REF, TARGET
from test_w502_my_card_fence_real_path import _control
from test_w502_person_control_binding import _create, _locator, _project_control, _service
from test_w580_bound_card_writers import redis_client  # noqa: F401 - fixture


async def _my(h):
    identity = ProjectPersonCardIdentity.build(project_ref=PROJECT_REF, person_subject=TARGET)
    return (await h.store.read_current_authority(subject_hash=subject_hash_for(TARGET),
                                                 access_id=identity.my_card_id))[1]


async def _legacy_world(h):
    """C issued before the project had a P (no binding), and a My Card whose pointer predates the holder."""
    h.port.locator = None
    created = await _create(h)
    assert created["ok"] is True and created["project_control_binding"] == "no_project_control"
    p_id = await _project_control(h)
    h.port.locator = _locator()
    my = await _my(h)
    older = dataclasses.replace(my, card_revision=my.card_revision + 1,
                                control_card=dataclasses.replace(my.control_card, holder_subject=""))
    await h.cards.commit(older, subject_hash=subject_hash_for(TARGET), expected_revision=my.card_revision,
                         now=int(time.time()))
    return p_id


def _plan_check(card, subject=TARGET):
    return _target_identity(card, PROJECT_REF, subject)[0]


@pytest.mark.asyncio
async def test_a_legacy_c_and_my_are_bound_as_new_cards_and_the_plan_check_passes(tmp_path, redis_client):  # noqa: F811
    h = await _service(tmp_path, redis_client)
    p_id = await _legacy_world(h)
    c_before, my_before = await _control(h), await _my(h)
    assert c_before.control_card is None and my_before.control_card.holder_subject == ""
    for card in (c_before, my_before):
        with pytest.raises(DecisionRefused, match="card_plan_update_scope_invalid"):
            _plan_check(card)

    counts = await repair_legacy_project_bindings(h.service, h.store)

    assert counts == {"c_bound": 1, "my_repaired": 1}
    c_after, my_after = await _control(h), await _my(h)
    assert c_after.card_revision == c_before.card_revision + 1 and c_after.control_card.control_id == p_id
    assert c_after.control_card.issuer_kind == "application" and c_after.control_card.issuer_ref == PROJECT_REF
    assert my_after.card_revision == my_before.card_revision + 1
    assert _plan_check(c_after) == "person_control" and _plan_check(my_after) == "my_card"
    # The selection, grants, identity and provenance are the legacy Card's own; only the binding moved.
    for before, after in ((c_before, c_after), (my_before, my_after)):
        for field in ("access_id", "grantor_subject", "issuer_kind", "issuer_ref", "resource_grants",
                      "resource_operations", "account_scope", "provenance", "state"):
            assert getattr(after, field) == getattr(before, field), field


@pytest.mark.asyncio
async def test_a_second_load_changes_nothing(tmp_path, redis_client):  # noqa: F811
    h = await _service(tmp_path, redis_client)
    await _legacy_world(h)
    await repair_legacy_project_bindings(h.service, h.store)
    c, my = await _control(h), await _my(h)
    again = await repair_legacy_project_bindings(h.service, h.store)
    assert again == {"my_already_bound": 1}
    assert (await _control(h)).card_revision == c.card_revision and (await _my(h)).card_revision == my.card_revision


@pytest.mark.asyncio
async def test_without_one_p_a_legacy_c_is_left_unbound(tmp_path, redis_client):  # noqa: F811
    h = await _service(tmp_path, redis_client)
    h.port.locator = None
    assert (await _create(h))["project_control_binding"] == "no_project_control"
    counts = await repair_legacy_project_bindings(h.service, h.store)
    assert counts.get("c_skipped_no_single_p") == 1 and "c_bound" not in counts
    assert (await _control(h)).control_card is None


@pytest.mark.asyncio
async def test_a_c_already_bound_to_a_p_is_not_touched(tmp_path, redis_client):  # noqa: F811
    h = await _service(tmp_path, redis_client)
    await _project_control(h)
    assert (await _create(h))["project_control_binding"] == "bound"
    c = await _control(h)
    counts = await repair_legacy_project_bindings(h.service, h.store)
    assert "c_bound" not in counts and (await _control(h)).card_revision == c.card_revision
    assert isinstance(c.control_card, ControlCardBinding)
