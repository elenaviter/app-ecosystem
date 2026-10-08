"""W578: a planned card group's whole Control chain composes before any write (genesis included).

Real P, C and My are built by the Hub's own paths (control_card_create and
the born-bound project-person create), then used as a PLANNED brand-new
project group: nothing of it is live, so every parent resolves from the group
itself. The qualified C -> P edge and P as the top boundary apply to the
planned P exactly as to a live one.
"""

from __future__ import annotations

import dataclasses

import pytest

from service_foundation.coordination.durable_decision_log import DecisionRefused

from connection_hub.delegated_credentials.cards.card_group import compose_group_chains, group_member
from connection_hub.delegated_credentials.cards.model import ControlCardBinding
from connection_hub.delegated_credentials.cards.store import subject_hash_for
from connection_hub.delegated_credentials.controls.project_person import ProjectPersonControlIdentity
from connection_hub.delegated_credentials.project_identity_lifecycle import ProjectPersonCardIdentity
from test_project_person_access import PROJECT_REF, TARGET
from test_w502_person_control_binding import CREATOR, _create, _project_control, _service
from test_w580_bound_card_writers import redis_client  # noqa: F401 - fixture


async def _planned_genesis(tmp_path, redis_client):  # noqa: F811
    """The real P, C and My of a project, as planned members (absent originals)."""
    h = await _service(tmp_path, redis_client)
    p_id = await _project_control(h)
    assert (await _create(h))["project_control_binding"] == "bound"
    c_identity = ProjectPersonControlIdentity.build(project_ref=PROJECT_REF, target_subject=TARGET)
    my_identity = ProjectPersonCardIdentity.build(project_ref=PROJECT_REF, person_subject=TARGET)

    async def current(subject_hash, access_id):
        return (await h.store.read_current_authority(subject_hash=subject_hash, access_id=access_id))[1]

    p = await current(subject_hash_for(CREATOR), p_id)
    c = await current(subject_hash_for(c_identity.project_subject), c_identity.control_id)
    my = await current(subject_hash_for(TARGET), my_identity.my_card_id)
    return p, c, my


def _as_planned(*cards):
    return [group_member(original=None, candidate=dataclasses.replace(card, card_revision=1), action="create")
            for card in cards]


async def _nothing_live(subject_hash, access_id):
    return None


@pytest.mark.asyncio
async def test_a_planned_p_c_and_my_compose_with_nothing_live(tmp_path, redis_client):  # noqa: F811
    p, c, my = await _planned_genesis(tmp_path, redis_client)
    await compose_group_chains(_as_planned(p, c, my), _nothing_live)


@pytest.mark.asyncio
async def test_a_planned_c_whose_p_is_neither_planned_nor_live_is_refused(tmp_path, redis_client):  # noqa: F811
    p, c, my = await _planned_genesis(tmp_path, redis_client)
    with pytest.raises(DecisionRefused, match="card_group_chain_invalid"):
        await compose_group_chains(_as_planned(c, my), _nothing_live)


@pytest.mark.asyncio
async def test_a_live_p_outside_the_group_is_the_join_case(tmp_path, redis_client):  # noqa: F811
    p, c, my = await _planned_genesis(tmp_path, redis_client)

    async def live_p(subject_hash, access_id):
        return p if (subject_hash, access_id) == (subject_hash_for(CREATOR), p.access_id) else None

    await compose_group_chains(_as_planned(c, my), live_p)


@pytest.mark.asyncio
async def test_a_planned_p_with_a_parent_is_not_the_top_boundary(tmp_path, redis_client):  # noqa: F811
    p, c, my = await _planned_genesis(tmp_path, redis_client)
    above = dataclasses.replace(p, access_id="control-above", issuer_ref="work:org:above")
    parented = dataclasses.replace(p, control_card=ControlCardBinding(
        control_id=above.access_id, issuer_ref=above.issuer_ref, issuer_kind="application", control_revision=1))
    with pytest.raises(DecisionRefused, match="card_group_chain_invalid") as refused:
        await compose_group_chains(_as_planned(above, parented, c, my), _nothing_live)
    assert getattr(refused.value.__cause__, "reason", "") == "project_control_not_root"


@pytest.mark.asyncio
async def test_a_planned_p_of_another_project_is_refused(tmp_path, redis_client):  # noqa: F811
    p, c, my = await _planned_genesis(tmp_path, redis_client)
    other = dataclasses.replace(p, issuer_ref="work:project:someone-else")
    with pytest.raises(DecisionRefused, match="card_group_chain_invalid"):
        await compose_group_chains(_as_planned(other, c, my), _nothing_live)
