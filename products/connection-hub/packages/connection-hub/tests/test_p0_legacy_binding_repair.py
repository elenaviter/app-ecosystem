"""P0, 10 Oct 2026: a legacy person Control (issued before W502 bound C under P) and a My Card with an older
holder are bound at bundle load exactly as new Cards are; the Card-edit PLAN check then passes; a second
load changes nothing.

Live 11:07Z every Control Card save was refused card_plan_update_scope_invalid by
``existing_card_selection_plan._target_identity`` (kept strict). Live probe 11:2xZ: all 7 person Controls
unbound, 4 My Cards with an older holder, one P per project.
"""
from __future__ import annotations

import dataclasses
import os
import pathlib
import time
import uuid
from types import SimpleNamespace

import pytest
from service_foundation.coordination.durable_decision_log import DecisionRefused

from connection_hub.delegated_credentials.cards.model import ControlCardBinding
from connection_hub.delegated_credentials.cards.store import subject_hash_for
from connection_hub.delegated_credentials.existing_card_selection_plan import _target_identity
from connection_hub.delegated_credentials import project_control_binding
from connection_hub.delegated_credentials.automation_access import LEGACY_BINDING_REPAIR
from connection_hub.delegated_credentials.controls.project_person import ProjectPersonControlIdentity
from connection_hub.delegated_credentials.legacy_binding_repair import repair_legacy_project_bindings
from connection_hub.delegated_credentials.project_identity_lifecycle import ProjectPersonCardIdentity

from test_project_person_access import PROJECT_REF, TARGET
from test_w502_my_card_fence_real_path import ADMIN, GRANT, OPERATION, _control
from test_w580_bound_card_writers import _memories
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


def _dsn() -> str:
    path = os.environ.get("CONNECTION_HUB_TEST_POSTGRES_DSN_FILE", "")
    return pathlib.Path(path).read_text().strip() if path else os.environ.get("CONNECTION_HUB_TEST_POSTGRES_DSN", "")


class _Grants:
    async def set_card_credentials_expiry(self, *args, **kwargs):
        return "applied"


async def _live_card_transactions(h):
    """As live (entrypoint._bind_card_transactions): the REAL coordinator on a PostgreSQL decision store,
    with the project scope managed (W578). Returns the pool to close."""
    import asyncpg
    from connection_hub.delegated_credentials.cards import composition

    pool = await asyncpg.create_pool(_dsn(), min_size=1, max_size=4)
    decisions = await composition.postgres_decision_store(pool, tenant=f"p0b{uuid.uuid4().hex[:10]}",
                                                          project="legacy-binding")
    composition.bind_card_transactions(
        h.service, persistence=SimpleNamespace(card_store=h.store, card_service=h.cards), decisions=decisions,
        grant_store=_Grants(), policies=None, managed_control_scopes=("work:project:",))
    return pool


@pytest.mark.asyncio
async def test_with_card_transactions_on_as_live_the_legacy_cards_are_still_bound(tmp_path, redis_client):  # noqa: F811
    # Review of f5d4d453 (claude-app@spark1, B1): live, attach refused the managed P and the repair bound nothing.
    if not _dsn():
        pytest.skip("needs CONNECTION_HUB_TEST_POSTGRES_DSN(_FILE): a disposable PostgreSQL")
    h = await _service(tmp_path, redis_client)
    p_id = await _legacy_world(h)
    pool = await _live_card_transactions(h)
    try:
        assert h.service._managed_direct_write_refused() is not None, "Card transactions are on, as live"
        # Without the repair's switch both gates refuse, as Spark App's probe showed.
        identity = ProjectPersonControlIdentity.build(project_ref=PROJECT_REF, target_subject=TARGET)
        plain = await project_control_binding.bind_project_control(h.service, identity, _locator())
        assert plain.get("error") == "card_transactions_direct_write_refused", plain
        assert (await _control(h)).control_card is None
        counts = await repair_legacy_project_bindings(h.service, h.store)
        assert counts == {"c_bound": 1, "my_repaired": 1}, counts
        c, my = await _control(h), await _my(h)
        assert c.control_card.control_id == p_id and _plan_check(c) == "person_control"
        assert _plan_check(my) == "my_card"
        assert (await repair_legacy_project_bindings(h.service, h.store)) == {"my_already_bound": 1}
        # The switch is the repair's alone: it is off again afterwards.
        assert LEGACY_BINDING_REPAIR.get() is False
    finally:
        await pool.close()


def _reselect(card, selection):
    return {"kind": "reselect", "target_subject": TARGET, "access_id": card.access_id,
            "subject_hash": subject_hash_for(card.grantor_subject), "original_revision": card.card_revision,
            "selection": selection}


async def _plan_save(h, card, selection, request_id):
    """The Card-edit PLAN exactly as the Hub's card_lifecycle_plan handler runs it, host-authorized."""
    from connection_hub.delegated_credentials.card_lifecycle_plan import plan_card_lifecycle
    from connection_hub.delegated_credentials.cards.lifecycle_plan_operation import UPDATE_STEPS
    from connection_hub.delegated_credentials.project_authorization import (
        LifecyclePlanAuthorization, LifecyclePlanAuthorizationRequest, LifecyclePlanStep, ProjectAuthorizationDecision,
    )
    updates = [_reselect(card, selection)]
    steps = (LifecyclePlanStep(ref="update:0", operation=UPDATE_STEPS["reselect"], target_subject=TARGET),)
    request = LifecyclePlanAuthorizationRequest(actor_subject=ADMIN, project_ref=PROJECT_REF, request_id=request_id,
                                                request_digest="a" * 64, steps=steps)
    authorization = LifecyclePlanAuthorization(request=request, decisions=tuple(
        (step.ref, ProjectAuthorizationDecision.allow(request.step_request(step), delegable_grants=(GRANT,),
                                                      platform_admin=True)) for step in steps))
    return await plan_card_lifecycle(h.service, project_ref=PROJECT_REF, creations=[], updates=updates,
                                     actor_subject=ADMIN, actor_kind="caller", request_id=request_id,
                                     authorization=authorization)


@pytest.mark.asyncio
async def test_after_the_repair_a_legacy_c_save_plans_and_commits_with_card_transactions_on(tmp_path, redis_client):  # noqa: F811
    """Main (#725): the operator's own save, end to end on the Hub, with Card transactions on as live."""
    from datetime import datetime, timezone
    from connection_hub.delegated_credentials.cards import transaction_store as tx
    from connection_hub.delegated_credentials.cards.model import CardAuthority
    from test_card_transaction_store import Decisions

    if not _dsn():
        pytest.skip("needs CONNECTION_HUB_TEST_POSTGRES_DSN(_FILE): a disposable PostgreSQL")
    h = await _service(tmp_path, redis_client)
    await _legacy_world(h)
    pool = await _live_card_transactions(h)
    try:
        legacy = await _control(h)
        resource = _memories()
        # An operator-like edit of the legacy C: select the project's grant and operation for one service.
        selection = {"resource_grants": {resource: [GRANT]}, "resource_operations": {resource: [OPERATION]}}
        refused = await _plan_save(h, legacy, selection, "save-before-repair")
        assert refused["ok"] is False and refused.get("error") == "card_plan_update_scope_invalid", refused

        await repair_legacy_project_bindings(h.service, h.store)
        bound = await _control(h)
        planned = await _plan_save(h, bound, selection, "save-after-repair")
        assert planned["ok"] is True, planned
        plan = planned["plan"]
        # As the participant stages it: each member with its exact original (here the repaired C).
        assert [(m["original_revision"], m["original_absent"]) for m in plan["candidate_value"]["cards"]] == [
            (bound.card_revision, False)]
        members = [(member["subject_hash"], bound, CardAuthority.from_mapping(member["candidate"]), member["action"])
                   for member in plan["candidate_value"]["cards"]]
        assert [(m[2].access_id, m[3]) for m in members] == [(bound.access_id, "update")]

        decisions = Decisions()
        tx.bind_transaction_decisions(h.store, decisions)  # the coordinator's recorded decision, as PB records it

        class _Reservations:  # live: the Hub's catalog store (entrypoint passes catalog_store)
            def __init__(self):
                self.held = []

            async def reserve(self, **kwargs):
                self.held.append(kwargs)

            async def release(self, transaction_id, *, intent_digest):
                self.held = [item for item in self.held if item["transaction_id"] != transaction_id]

        tx.bind_catalog_reservations(h.store, _Reservations())
        group_id, intent = "e" * 64, "f" * 64
        now = int(time.time())
        staged = await h.cards.stage_group_transaction(
            transaction_id=group_id, intent_digest=intent, participant="project", members=members,
            now=datetime.fromtimestamp(now, timezone.utc), reads=plan["reads"], catalog=plan["catalog_digest"])
        assert staged["staged"] is True, staged
        decisions.recorded[group_id] = "committed"
        decided = await h.cards.decide_group_transaction(transaction_id=group_id, intent_digest=intent,
                                                         decision="committed", now=now)
        assert decided["state"] == "committed", decided
        saved = await _control(h)
        assert saved.card_revision == bound.card_revision + 1
        assert list(saved.resource_grants.get(resource, ())) == [GRANT]
        assert list(saved.resource_operations.get(resource, ())) == [OPERATION]
        assert saved.control_card == bound.control_card, "the save keeps the repaired link"
    finally:
        await pool.close()
