"""#725 review probes by codex-infra@e-home (10 Oct 2026), kept as regression tests: memory I/O and CAS
stand-ins that execute the actual repair, bind helper, My repair, direct-write guard predicates and marker
functions. No productive coordinator, PostgreSQL or Redis here; test_p0_legacy_binding_repair.py runs the
live configuration.
"""
import dataclasses
import logging
import pathlib
from collections import Counter
from types import SimpleNamespace

import pytest
from connection_hub.delegated_credentials import legacy_binding_repair as repair
from connection_hub.delegated_credentials.automation_access import (
    LEGACY_BINDING_REPAIR, AutomationAccessService, _legacy_unbound_c_to_its_root_p,
    card_authority_from_record, record_from_card)
from connection_hub.delegated_credentials.caller_writer_gate import CallerWriteRefused
from connection_hub.delegated_credentials.cards.model import CARD_STATE_ACTIVE, CardCredentialHandles, ControlCardBinding
from connection_hub.delegated_credentials.cards.store import subject_hash_for
from connection_hub.delegated_credentials.controls.model import control_card_id_for_issuer
from connection_hub.delegated_credentials.controls.project_person import ProjectPersonControlIdentity, bind_project_person_control
from connection_hub.delegated_credentials.project_identity_lifecycle import new_project_person_my_card
from test_project_person_control import _authority, PROJECT_REF, TARGET

class Memory:
    def __init__(self, cards, root):
        self.root = pathlib.Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.records = {(card.grantor_subject, card.access_id): record_from_card(card) for card in cards}
        self.writes, self.reads, self.scans = [], 0, 0
        self._card_coordinator = object()
    _BOUND_AUTHORITY_FIELDS = AutomationAccessService._BOUND_AUTHORITY_FIELDS
    _refuse_bound_direct_write = AutomationAccessService._refuse_bound_direct_write
    async def list_grantor_hashes(self):
        self.scans += 1
        return sorted({subject_hash_for(holder) for holder, _ in self.records})
    async def list_card_ids(self, *, subject_hash):
        return [access for holder, access in self.records if subject_hash_for(holder) == subject_hash]
    async def read_current_authority(self, *, subject_hash, access_id):
        self.reads += 1
        record = next(record for (holder, access), record in self.records.items()
            if subject_hash_for(holder) == subject_hash and access == access_id)
        return None, card_authority_from_record(record)
    async def _load_record_any_state(self, access_id, *, grantor_subject):
        record = self.records.get((grantor_subject, access_id))
        return None if record is None else (record, CARD_STATE_ACTIVE)
    def _cards(self):
        async def load(access_id, *, subject_hash):
            found = await self.read_current_authority(subject_hash=subject_hash, access_id=access_id)
            return found[1], CardCredentialHandles(access_id=access_id)
        return SimpleNamespace(load=load)
    async def _persist_record(self, record, *, expected_revision):
        # Execute the actual enabled direct-write guard, then synthetic CAS/persist only.
        # This is NOT _persist_record/_coordinated_write/backend qualification.
        await self._refuse_bound_direct_write(record, card_authority_from_record(record),
                                              expected_revision=expected_revision)
        key = record.grantor_subject, record.access_id
        assert self.records[key].card_revision == expected_revision
        self.records[key] = record
        self.writes.append(record.access_id)
    async def attach_control_card(self, *args, **kwargs):
        # Real bind_project_control forwards an open policy refusal as-is.
        return CallerWriteRefused("synthetic-private-refusal-value").to_dict()

def world(root, roots=1, *, legacy=True, c_binding_change=None):
    identity = ProjectPersonControlIdentity.build(project_ref=PROJECT_REF, target_subject=TARGET)
    c = bind_project_person_control(_authority(), identity=identity)
    def p(holder):
        return dataclasses.replace(c, access_id=control_card_id_for_issuer("application", PROJECT_REF,
            grantor_subject=holder), grantor_subject=holder, issuer_kind="application",
            control_card=None, properties={})
    primary = p("synthetic-owner-a")
    c = dataclasses.replace(c, control_card=ControlCardBinding(control_id=primary.access_id,
        issuer_ref=PROJECT_REF, issuer_kind="application", control_revision=primary.card_revision,
        holder_subject=primary.grantor_subject))
    if c_binding_change:
        c = dataclasses.replace(c, control_card=dataclasses.replace(c.control_card, **c_binding_change))
    my = new_project_person_my_card(control_card=c)
    if legacy:
        my = dataclasses.replace(my, control_card=dataclasses.replace(my.control_card, holder_subject=""))
    cards = [c, my] + ([primary] if roots else []) + ([p("synthetic-owner-b")] if roots == 2 else [])
    return Memory(cards, root), c, my, primary

@pytest.mark.asyncio
@pytest.mark.parametrize("roots", [0, 2])
async def test_zero_or_multiple_roots_now_leave_my_untouched(tmp_path, roots):
    host, _, _, _ = world(tmp_path, roots)
    counts = await repair.repair_legacy_project_bindings(host, host)
    assert counts == {"my_skipped_no_single_p": 1}
    assert host.writes == [] and not repair._complete(host)
    assert LEGACY_BINDING_REPAIR.get() is False

@pytest.mark.asyncio
async def test_foreign_control_id_now_leaves_my_untouched(tmp_path):
    host, _, _, _ = world(tmp_path, c_binding_change={"control_id": "synthetic-foreign-p"})
    assert await repair.repair_legacy_project_bindings(host, host) == {"my_skipped_c_not_under_root_p": 1}
    assert host.writes == [] and not repair._complete(host)

@pytest.mark.asyncio
async def test_actual_direct_guard_allows_repair_binding_only_and_resets_afterwards(tmp_path):
    host, _, my, _ = world(tmp_path)
    counts = await repair.repair_legacy_project_bindings(host, host)
    assert counts == {"my_repaired": 1} and host.writes == [my.access_id]
    assert LEGACY_BINDING_REPAIR.get() is False
    current = host.records[(my.grantor_subject, my.access_id)]
    bad = dataclasses.replace(current, control_card=dataclasses.replace(current.control_card, holder_subject=""))
    with pytest.raises(CallerWriteRefused, match="card_transactions_direct_write_refused"):
        await host._refuse_bound_direct_write(bad, card_authority_from_record(bad),
                                             expected_revision=current.card_revision)

def test_c_managed_parent_exception_is_off_outside_repair(tmp_path):
    host, c, _, p = world(tmp_path)
    unbound = dataclasses.replace(c, control_card=None)
    assert not _legacy_unbound_c_to_its_root_p(unbound, p)
    token = LEGACY_BINDING_REPAIR.set(True)
    try:
        assert _legacy_unbound_c_to_its_root_p(unbound, p)
        assert not _legacy_unbound_c_to_its_root_p(c, p)
    finally:
        LEGACY_BINDING_REPAIR.reset(token)
    assert LEGACY_BINDING_REPAIR.get() is False

@pytest.mark.asyncio
async def test_successful_marker_now_eliminates_second_scan(tmp_path):
    host, _, _, _ = world(tmp_path, legacy=False)
    await repair.repair_legacy_project_bindings(host, host)
    reads, scans = host.reads, host.scans
    assert await repair.repair_legacy_project_bindings(host, host) == {"already_complete": 1}
    assert (host.reads, host.scans) == (reads, scans)

@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [{"holder_subject": "synthetic-wrong-holder"},
                                    {"issuer_kind": "project"}])
async def test_c_must_match_the_whole_selected_root_locator_before_my_repair(tmp_path, changed):
    host, _, _, _ = world(tmp_path, c_binding_change=changed)
    await repair.repair_legacy_project_bindings(host, host)
    assert host.writes == [], "My repaired although C's P pointer has the wrong holder or issuer kind"

@pytest.mark.asyncio
async def test_refused_policy_value_is_not_logged_as_a_count_key(tmp_path, caplog):
    host, c, _, _ = world(tmp_path)
    host.records[(c.grantor_subject, c.access_id)] = dataclasses.replace(
        host.records[(c.grantor_subject, c.access_id)], control_card=None)
    caplog.set_level(logging.INFO, logger=repair.LOGGER.name)
    await repair.repair_legacy_project_bindings(host, host)
    assert "synthetic-private-refusal-value" not in caplog.text, "open refusal text entered count-key/log"

def test_two_process_marker_publications_do_not_share_one_consumable_tmp(tmp_path, monkeypatch):
    host = SimpleNamespace(root=tmp_path)
    original_replace = repair.os.replace
    entered = False
    def overlap(source, destination):
        nonlocal entered
        if not entered:
            entered = True
            repair._write_marker(host, {"my_already_bound": 1})  # deterministic process-B interleaving
        return original_replace(source, destination)
    monkeypatch.setattr(repair.os, "replace", overlap)
    repair._write_marker(host, {"my_already_bound": 1})  # A must not fail after B publishes
