# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""W389: My Card and the person's Control Card are read together within one request.

My Card is still decided first, the early Control Card read is discarded on every refusal or exit,
and nothing is kept between requests (codex-infra's conditions on W389).
"""

from __future__ import annotations

import asyncio
import dataclasses
import gc

import pytest

from connection_hub.delegated_credentials.cards.model import CARD_STATE_ACTIVE, CARD_STATE_REVOKED, ControlCardBinding
from connection_hub.delegated_credentials.controls.model import new_credentialless_card
from connection_hub.delegated_credentials.controls.project_person import ProjectPersonControlIdentity
from connection_hub.delegated_credentials.project_identity_lifecycle import (
    PROJECT_IDENTITY_EDGE_PROVENANCE,
    ProjectIdentityLifecycleError,
    ProjectPersonCardIdentity,
)

from test_project_person_access import PROJECT_REF, TARGET, _Host, _Port, _Record, _create, _lifecycle


class _Gated(_Host):
    """Reads can be held, failed or watched per Card id."""

    def __init__(self) -> None:
        super().__init__()
        self.started: list[str] = []
        self.gates: dict[str, asyncio.Event] = {}
        self.failures: dict[str, BaseException] = {}
        self.cancelled: list[str] = []
        self.after_first: dict[str, tuple] = {}

    async def _load_record_any_state(self, access_id, *, grantor_subject):
        self.started.append(access_id)
        try:
            if access_id in self.gates:
                await self.gates[access_id].wait()
        except asyncio.CancelledError:
            self.cancelled.append(access_id)
            raise
        if access_id in self.failures:
            raise self.failures[access_id]
        loaded = self.records.get((grantor_subject, access_id))
        if access_id in self.after_first and self.started.count(access_id) > 1:
            return self.after_first[access_id]
        return loaded


def _ids():
    control = ProjectPersonControlIdentity.build(project_ref=PROJECT_REF, target_subject=TARGET)
    person = ProjectPersonCardIdentity.build(project_ref=PROJECT_REF, person_subject=TARGET)
    return control.project_subject, control.control_id, person.my_card_id


async def _rig(*, chain: bool = False):
    host = _Gated()
    lifecycle = _lifecycle(host, _Port())
    await _create(lifecycle, migration=chain)
    if chain:
        # As in test_human_lifecycle_reads_current_ancestor_without_my_rewrite: C is bound to a parent.
        project, control_id, _my = _ids()
        child, _ = host.records[(project, control_id)]
        parent = new_credentialless_card(grantor_subject=project, catalog_version=child.catalog_version,
            control_id="ancestor", issuer_ref="issuer:ancestor", issuer_kind="service",
            initial_selection=child.authority, now=1)
        host.records[(project, control_id)] = (_Record(dataclasses.replace(child.authority, composition_mode="or",
            control_card=ControlCardBinding(control_id=parent.access_id, issuer_ref=parent.issuer_ref,
                issuer_kind=parent.issuer_kind, control_revision=parent.card_revision))), CARD_STATE_ACTIVE)
        host.records[(project, parent.access_id)] = (_Record(parent), CARD_STATE_ACTIVE)
    host.started.clear()
    return host, lifecycle._project_identities


def OUTCOME(resolution):
    return (resolution.edge is not None, resolution.control_card.authority is not None,
            getattr(resolution.control_card.authority, "state", None), resolution.control_card.reason)


# The outcomes main eb322f8b gives (edge present, C present, C state, reason); unchanged here.
MISSING_C = (True, False, None, "")
REVOKED_C = (True, True, CARD_STATE_REVOKED, "")


def _resolve(identities):
    return identities.resolve(project_ref=PROJECT_REF, person_subject=TARGET)


async def _no_unhandled(run):
    loop = asyncio.get_running_loop()
    seen: list[dict] = []
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: seen.append(context))
    try:
        result = await run()
    finally:
        await asyncio.sleep(0)
        gc.collect()
        await asyncio.sleep(0)
        loop.set_exception_handler(previous)
    assert seen == [], seen
    return result


def _own_tasks():
    return {task for task in asyncio.all_tasks() if task is not asyncio.current_task()}


async def test_my_card_and_the_control_card_are_read_at_the_same_time():
    host, identities = await _rig(chain=True)
    _project, control_id, my_id = _ids()
    host.gates = {my_id: asyncio.Event(), control_id: asyncio.Event()}
    pending = asyncio.ensure_future(_resolve(identities))
    for _ in range(5):
        await asyncio.sleep(0)
    assert host.started[:2] in ([my_id, control_id], [control_id, my_id]), host.started
    for gate in host.gates.values():
        gate.set()
    resolution = await pending
    assert resolution.edge is not None
    assert resolution.my_card.authority is not None and resolution.control_card.authority is not None
    assert resolution.control_card.reason == ""
    assert host.started.count(control_id) == 2, "the walk takes the early read; the fence still re-reads it"
    assert host.started.count("ancestor") == 2


async def test_a_missing_my_card_is_missing_even_when_the_control_card_read_fails():
    host, identities = await _rig()
    _project, control_id, my_id = _ids()
    del host.records[(TARGET, my_id)]
    host.failures[control_id] = RuntimeError("synthetic control store failure")
    resolution = await _no_unhandled(lambda: _resolve(identities))
    assert resolution.edge is None
    assert resolution.my_card.authority is None and resolution.control_card.authority is None


async def test_a_missing_my_card_does_not_wait_for_a_slow_control_card_read():
    host, identities = await _rig()
    _project, control_id, my_id = _ids()
    del host.records[(TARGET, my_id)]
    host.gates[control_id] = asyncio.Event()
    resolution = await asyncio.wait_for(_resolve(identities), timeout=2)
    assert resolution.edge is None
    assert host.cancelled in ([], [control_id]), "cancelled while waiting, or before it began"
    assert _own_tasks() == set()


async def test_an_invalid_my_card_gives_its_own_reason_not_the_control_card_failure():
    host, identities = await _rig()
    _project, control_id, my_id = _ids()
    record, state = host.records[(TARGET, my_id)]
    provenance = {k: v for k, v in dict(record.authority.provenance or {}).items()
                  if k != PROJECT_IDENTITY_EDGE_PROVENANCE}
    host.records[(TARGET, my_id)] = (_Record(dataclasses.replace(record.authority, provenance=provenance)), state)
    host.failures[control_id] = RuntimeError("synthetic control store failure")
    with pytest.raises(ProjectIdentityLifecycleError, match="project_identity_edge_marker_missing"):
        await _no_unhandled(lambda: _resolve(identities))


async def test_a_valid_my_card_reports_the_control_card_read_failure_as_before():
    host, identities = await _rig()
    _project, control_id, _my = _ids()
    host.failures[control_id] = RuntimeError("synthetic control store failure")
    with pytest.raises(RuntimeError, match="synthetic control store failure"):
        await _no_unhandled(lambda: _resolve(identities))


async def test_a_missing_control_card_resolves_as_before():
    host, identities = await _rig()
    project, control_id, _my = _ids()
    del host.records[(project, control_id)]
    resolution = await _resolve(identities)
    assert OUTCOME(resolution) == MISSING_C


async def test_a_revoked_control_card_resolves_as_before():
    host, identities = await _rig()
    project, control_id, _my = _ids()
    record, _state = host.records[(project, control_id)]
    host.records[(project, control_id)] = (record, CARD_STATE_REVOKED)
    resolution = await _resolve(identities)
    assert OUTCOME(resolution) == REVOKED_C


async def test_a_control_card_changed_after_the_early_read_is_refused_by_the_fence():
    host, identities = await _rig(chain=True)
    project, control_id, _my = _ids()
    record, state = host.records[(project, control_id)]
    host.after_first[control_id] = (
        _Record(dataclasses.replace(record.authority, card_revision=record.authority.card_revision + 1)), state)
    resolution = await _resolve(identities)
    assert resolution.control_card.reason == "control_card_dependency_changed"


async def test_cancelling_the_request_stops_both_reads():
    host, identities = await _rig()
    _project, control_id, my_id = _ids()
    host.gates = {my_id: asyncio.Event(), control_id: asyncio.Event()}
    pending = asyncio.ensure_future(_resolve(identities))
    for _ in range(5):
        await asyncio.sleep(0)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert sorted(host.cancelled) == sorted([my_id, control_id])
    assert _own_tasks() == set()


async def test_nothing_is_kept_between_requests():
    host, identities = await _rig(chain=True)
    project, control_id, my_id = _ids()
    await _resolve(identities)
    host.started.clear()
    await _resolve(identities)
    assert host.started.count(my_id) == 1 and host.started.count(control_id) == 2
    del host.records[(project, control_id)]
    assert OUTCOME(await _resolve(identities)) == MISSING_C, "the next request reads storage again"


async def test_a_parent_changed_during_the_fence_is_refused():
    host, identities = await _rig(chain=True)
    project, _control, _my = _ids()
    parent, state = host.records[(project, "ancestor")]
    host.after_first["ancestor"] = (
        _Record(dataclasses.replace(parent.authority, card_revision=parent.authority.card_revision + 1)), state)
    resolution = await _resolve(identities)
    assert resolution.control_card.reason == "control_card_dependency_changed"
