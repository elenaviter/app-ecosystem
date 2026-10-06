"""W578: several Cards staged under ONE transaction decision, at the store (card groups).

The aggregate receipt is written before any member and marked ``staged``
only after every member is; members are ordinary Card receipts under ids
derived from the group's id, asking the coordinator by the GROUP's id only;
finish decides every member and writes the aggregate last. Covered here:
invisibility while undecided, commit and abort for every member (an absent
original reads absent again after an abort), every crash point between the
aggregate and its last member, a fresh restage of an aborted-only slot
(EMain Q1), the absent-slot guard, the lead's effects gating every member,
and the per-Card fences.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from connection_hub.delegated_credentials.cards import transaction_store as tx
from connection_hub.delegated_credentials.cards.store import CardStorageError
from test_card_transaction_store import INTENT, NOW, SUBJECT_HASH, _setup

GROUP = "c" * 64
WHEN = NOW if isinstance(NOW, datetime) else datetime.fromtimestamp(NOW, timezone.utc)
EFFECTS = [{"kind": "grant_binding", "key": "tok-1", "payload": {"token": "t"}}]


def _members(before, after):
    """An existing Card's update and a newly minted Card, sorted as the group orders them."""
    created = replace(after, access_id="aut_zz_new", card_revision=1, label="created in the group")
    return sorted([(before, after), (None, created)], key=lambda pair: (SUBJECT_HASH, pair[1].access_id))


async def _begin(store, members, transaction_id=GROUP):
    return await tx.begin_group(store, transaction_id=transaction_id, intent_digest=INTENT, participant="project",
                                members=[(SUBJECT_HASH, candidate.access_id) for _, candidate in members])


async def _stage_member(store, members, index, transaction_id=GROUP, **extra):
    group = await tx.read_receipt(store, transaction_id)
    original, candidate = members[index]
    return await tx.stage(store, transaction_id=tx.member_transaction_id(transaction_id, index),
                          intent_digest=INTENT, participant="project", subject_hash=SUBJECT_HASH,
                          original=original, candidate=candidate, now=WHEN,
                          group=tx.group_member_ref(group, index), **extra)


async def _stage_group(store, members, transaction_id=GROUP, lead_effects=()):
    await _begin(store, members, transaction_id)
    for index in range(len(members)):
        await _stage_member(store, members, index, transaction_id,
                            **({"effects": list(lead_effects)} if index == 0 and lead_effects else {}))
    return await tx.complete_group(store, transaction_id=transaction_id, intent_digest=INTENT)


async def _finish(store, members, decision, transaction_id=GROUP):
    store._card_transaction_decisions.recorded[transaction_id] = decision
    for index in range(len(members)):
        member_id = tx.member_transaction_id(transaction_id, index)
        if await tx.read_receipt(store, member_id) is not None:
            await tx.decide(store, transaction_id=member_id, intent_digest=INTENT, decision=decision)
    return await tx.finish_group(store, transaction_id=transaction_id, intent_digest=INTENT, decision=decision)


async def _read(store, card):
    found = await store.read_current_authority(subject_hash=SUBJECT_HASH, access_id=card.access_id)
    return None if found is None else found[1]


@pytest.mark.asyncio
async def test_a_staged_group_is_invisible_until_its_one_decision(tmp_path):
    store, _, before, after = await _setup(tmp_path)
    members = _members(before, after)
    group = await _stage_group(store, members)
    assert group["staged"] is True and len(group["members"]) == 2
    for _, candidate in members:
        with pytest.raises(CardStorageError, match="card_transaction_undecided"):
            await _read(store, candidate)
    # Recovery sees the group by its own id; its members have no entries of their own.
    assert [entry["transaction_id"] for entry in await tx.list_in_doubt(store)] == [GROUP]


@pytest.mark.asyncio
async def test_the_recorded_commit_shows_every_member_even_before_local_finish(tmp_path):
    store, _, before, after = await _setup(tmp_path)
    members = _members(before, after)
    await _stage_group(store, members)
    store._card_transaction_decisions.recorded[GROUP] = "committed"
    assert [await _read(store, candidate) for _, candidate in members] == [candidate for _, candidate in members]
    decided = await _finish(store, members, "committed")
    assert decided["state"] == "committed" and await tx.list_in_doubt(store) == []
    assert [await _read(store, candidate) for _, candidate in members] == [candidate for _, candidate in members]


@pytest.mark.asyncio
async def test_an_abort_restores_every_member_and_the_new_slot_reads_absent(tmp_path):
    store, _, before, after = await _setup(tmp_path)
    members = _members(before, after)
    await _stage_group(store, members)
    await _finish(store, members, "aborted")
    created = next(candidate for original, candidate in members if original is None)
    assert await _read(store, before) == before and await _read(store, created) is None
    assert await store.read_current(subject_hash=SUBJECT_HASH, access_id=created.access_id) is None
    assert await tx.list_in_doubt(store) == []


@pytest.mark.asyncio
async def test_an_aborted_only_slot_is_restaged_fresh_by_a_new_transaction(tmp_path):
    """EMain Q1: the aborted staged revision is not history, so the same new id stages again."""
    store, _, before, after = await _setup(tmp_path)
    members = _members(before, after)
    await _stage_group(store, members)
    await _finish(store, members, "aborted")
    retry = "d" * 64
    group = await _stage_group(store, members, transaction_id=retry)
    assert group["staged"] is True
    await _finish(store, members, "committed", transaction_id=retry)
    created = next(candidate for original, candidate in members if original is None)
    assert await _read(store, created) == created


@pytest.mark.asyncio
async def test_a_slot_with_committed_history_is_never_staged_as_absent(tmp_path):
    store, _, before, after = await _setup(tmp_path)
    members = [(None, replace(after, card_revision=1))]  # the harness Card exists at r1
    await _begin(store, members)
    with pytest.raises(tx.CardTransactionRefused, match="card_transaction_absent_slot_used"):
        await _stage_member(store, members, 0)


@pytest.mark.asyncio
async def test_an_old_id_whose_pointer_is_gone_is_never_recreated(tmp_path):
    """CodeApp 22:59: an id with committed history is never staged as absent, even without a pointer."""
    store, _, before, after = await _setup(tmp_path)
    store.current_path(subject_hash=SUBJECT_HASH, access_id=before.access_id).unlink()
    assert await store.read_current(subject_hash=SUBJECT_HASH, access_id=before.access_id) is None
    members = [(None, replace(after, card_revision=1))]
    await _begin(store, members)
    with pytest.raises(tx.CardTransactionRefused, match="card_transaction_absent_slot_used"):
        await _stage_member(store, members, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("staged_members", [0, 1])
async def test_a_group_interrupted_before_complete_is_never_committed_and_aborts_cleanly(tmp_path, staged_members):
    store, _, before, after = await _setup(tmp_path)
    members = _members(before, after)
    await _begin(store, members)
    for index in range(staged_members):
        await _stage_member(store, members, index)
    with pytest.raises(tx.CardTransactionRefused, match="card_transaction_group_incomplete"):
        await tx.complete_group(store, transaction_id=GROUP, intent_digest=INTENT)
    [entry] = await tx.list_in_doubt(store)
    assert entry["transaction_id"] == GROUP and entry["staged"] is False
    store._card_transaction_decisions.recorded[GROUP] = "committed"
    with pytest.raises(tx.CardTransactionRefused, match="card_transaction_not_staged"):
        await tx.finish_group(store, transaction_id=GROUP, intent_digest=INTENT, decision="committed")
    await _finish(store, members, "aborted")
    created = next(candidate for original, candidate in members if original is None)
    assert await _read(store, before) == before and await _read(store, created) is None
    assert await tx.list_in_doubt(store) == []


@pytest.mark.asyncio
async def test_the_aggregate_is_written_last_and_finish_is_idempotent_after_a_crash(tmp_path):
    store, _, before, after = await _setup(tmp_path)
    members = _members(before, after)
    await _stage_group(store, members)
    store._card_transaction_decisions.recorded[GROUP] = "committed"
    # Crash after the first member was decided, before the aggregate: still in doubt.
    await tx.decide(store, transaction_id=tx.member_transaction_id(GROUP, 0), intent_digest=INTENT,
                    decision="committed")
    with pytest.raises(tx.CardTransactionRefused, match="card_transaction_group_members_pending"):
        await tx.finish_group(store, transaction_id=GROUP, intent_digest=INTENT, decision="committed")
    assert [entry["transaction_id"] for entry in await tx.list_in_doubt(store)] == [GROUP]
    first = await _finish(store, members, "committed")
    again = await _finish(store, members, "committed")
    assert first == again and again["state"] == "committed"
    with pytest.raises(tx.CardTransactionRefused, match="card_transaction_decision_conflict"):
        await tx.finish_group(store, transaction_id=GROUP, intent_digest=INTENT, decision="aborted")


@pytest.mark.asyncio
async def test_a_group_is_finished_only_as_a_group(tmp_path):
    store, _, before, after = await _setup(tmp_path)
    members = _members(before, after)
    await _stage_group(store, members)
    store._card_transaction_decisions.recorded[GROUP] = "committed"
    with pytest.raises(tx.CardTransactionRefused, match="card_transaction_is_group"):
        await tx.decide(store, transaction_id=GROUP, intent_digest=INTENT, decision="committed")
    with pytest.raises(tx.CardTransactionRefused, match="card_transaction_replay_changed"):
        await tx.stage(store, transaction_id=GROUP, intent_digest=INTENT, participant="project",
                       subject_hash=SUBJECT_HASH, original=before, candidate=after, now=WHEN)
    # A member never answers to a decision recorded under its own derived id.
    member_id = tx.member_transaction_id(GROUP, 0)
    store._card_transaction_decisions.recorded.pop(GROUP)
    store._card_transaction_decisions.recorded[member_id] = "committed"
    with pytest.raises(tx.CardTransactionRefused, match="card_transaction_decision_not_recorded"):
        await tx.decide(store, transaction_id=member_id, intent_digest=INTENT, decision="committed")


@pytest.mark.asyncio
async def test_staged_members_are_fenced_including_the_new_slot(tmp_path):
    store, _, before, after = await _setup(tmp_path)
    members = _members(before, after)
    await _stage_group(store, members)
    for _, candidate in members:
        with pytest.raises(CardStorageError, match="card_transaction_unresolved"):
            await tx.assert_replaceable(store, subject_hash=SUBJECT_HASH, access_id=candidate.access_id)


@pytest.mark.asyncio
async def test_the_leads_effects_gate_every_member(tmp_path):
    store, _, before, after = await _setup(tmp_path)
    members = _members(before, after)
    await _stage_group(store, members, lead_effects=EFFECTS)
    store._card_transaction_decisions.recorded[GROUP] = "committed"
    for _, candidate in members:
        with pytest.raises(CardStorageError, match="card_effects_pending"):
            await _read(store, candidate)
    lead = await tx.decide(store, transaction_id=tx.member_transaction_id(GROUP, 0), intent_digest=INTENT,
                           decision="committed")
    applied = []

    async def apply(kind, key, payload, *, transaction_id):
        applied.append((transaction_id, kind, key))

    await tx.apply_effects(store, lead, apply)
    assert applied == [(tx.member_transaction_id(GROUP, 0), "grant_binding", "tok-1")]
    await _finish(store, members, "committed")
    assert [await _read(store, candidate) for _, candidate in members] == [candidate for _, candidate in members]


@pytest.mark.asyncio
async def test_only_the_lead_member_may_carry_reads_catalog_or_effects(tmp_path):
    store, _, before, after = await _setup(tmp_path)
    members = _members(before, after)
    await _begin(store, members)
    await _stage_member(store, members, 0)
    with pytest.raises(CardStorageError, match="card_transaction_receipt_invalid"):
        await _stage_member(store, members, 1, effects=EFFECTS)


@pytest.mark.asyncio
async def test_a_changed_group_replay_is_refused(tmp_path):
    store, _, before, after = await _setup(tmp_path)
    members = _members(before, after)
    await _begin(store, members)
    with pytest.raises(tx.CardTransactionRefused, match="card_transaction_replay_changed"):
        await tx.begin_group(store, transaction_id=GROUP, intent_digest="e" * 64, participant="project",
                             members=[(SUBJECT_HASH, candidate.access_id) for _, candidate in members])
    assert (await _begin(store, members))["staged"] is False  # the exact replay is the same receipt


# ── the Card service: one group, its sections, per-member action checks, lead first ──

def _service_members(before, after, *, action="update"):
    created = replace(after, access_id="aut_zz_new", card_revision=1, label="created in the group")
    pairs = [(SUBJECT_HASH, before, after, action), (SUBJECT_HASH, None, created, "create")]
    return sorted(pairs, key=lambda member: (member[0], member[2].access_id))


async def _service_stage(service, members, **extra):
    return await service.stage_group_transaction(transaction_id=GROUP, intent_digest=INTENT, participant="project",
                                                 members=members, now=WHEN, **extra)


async def _service_finish(store, service, decision):
    store._card_transaction_decisions.recorded[GROUP] = decision
    return await service.decide_group_transaction(transaction_id=GROUP, intent_digest=INTENT, decision=decision,
                                                  now=NOW if isinstance(NOW, int) else None)


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["committed", "aborted"])
async def test_the_service_stages_and_finishes_a_group_under_one_decision(tmp_path, decision):
    store, service, before, after = await _setup(tmp_path)
    members = _service_members(before, after)
    group = await _service_stage(service, members)
    assert group["staged"] is True
    decided = await _service_finish(store, service, decision)
    assert decided["state"] == decision and await tx.list_in_doubt(store) == []
    created = members[-1][2] if members[-1][1] is None else members[0][2]
    if decision == "committed":
        assert await _read(store, before) == after and await _read(store, created) == created
    else:
        assert await _read(store, before) == before and await _read(store, created) is None


def _bound(card, issuer_ref="work:project:one", control_id="control-1"):
    from connection_hub.delegated_credentials.cards.model import ControlCardBinding

    return replace(card, control_card=ControlCardBinding(control_id=control_id, issuer_ref=issuer_ref,
                                                         issuer_kind="project", control_revision=1))


@pytest.mark.asyncio
@pytest.mark.parametrize("case,reason", [
    ("create_over_existing", "card_group_member_action_invalid"),
    ("update_of_absent", "card_group_member_action_invalid"),
    ("update_binds", "caller_writer_binding_change_refused"),
    ("update_changes_identity", "caller_writer_candidate_binding_mismatch"),
    ("revoke_still_active", "card_group_revoke_not_ended"),
    ("attach_moves_issuer", "caller_writer_binding_change_refused"),
])
async def test_each_members_action_is_checked_against_its_original_and_nothing_is_staged(tmp_path, case, reason):
    """EMain Q3: the gate's binding and shape rules hold for every member, before any write."""
    store, service, before, after = await _setup(tmp_path)
    created = replace(after, access_id="aut_zz_new", card_revision=1)
    member = {
        "create_over_existing": (SUBJECT_HASH, before, after, "create"),
        "update_of_absent": (SUBJECT_HASH, None, created, "update"),
        "update_binds": (SUBJECT_HASH, before, _bound(after), "update"),
        "update_changes_identity": (SUBJECT_HASH, before, replace(after, issuer_kind="other"), "update"),
        "revoke_still_active": (SUBJECT_HASH, before, after, "revoke"),
        "attach_moves_issuer": None,
    }[case]
    if case == "attach_moves_issuer":
        bound_before = _bound(replace(before, card_revision=before.card_revision + 1))
        await service.commit(bound_before, subject_hash=SUBJECT_HASH, expected_revision=before.card_revision,
                             now=NOW)
        member = (SUBJECT_HASH, bound_before,
                  _bound(replace(bound_before, card_revision=bound_before.card_revision + 1),
                         issuer_ref="work:project:other", control_id="control-2"), "attach")
    with pytest.raises(tx.CardTransactionRefused, match=f"^{reason}$"):
        await _service_stage(service, [member])
    assert await tx.read_receipt(store, GROUP) is None and await tx.list_in_doubt(store) == []


@pytest.mark.asyncio
async def test_a_revoke_member_publishes_only_an_ended_card(tmp_path):
    from connection_hub.delegated_credentials.cards.model import CARD_STATE_REVOKED

    store, service, before, after = await _setup(tmp_path)
    revoked = replace(before, card_revision=before.card_revision + 1, state=CARD_STATE_REVOKED)
    await _service_stage(service, [(SUBJECT_HASH, before, revoked, "revoke")])
    await _service_finish(store, service, "committed")
    current = await store.read_current_authority(subject_hash=SUBJECT_HASH, access_id=before.access_id)
    assert current[1].state == CARD_STATE_REVOKED


@pytest.mark.asyncio
async def test_the_lead_effects_apply_before_any_member_is_served(tmp_path):
    from test_card_transaction_store import _Applier

    store, service, before, after = await _setup(tmp_path)
    events = []

    class _Ordered(_Applier):
        async def __call__(self, kind, key, payload, *, transaction_id):
            events.append(("effect", key))
            await super().__call__(kind, key, payload, transaction_id=transaction_id)

    applier = _Ordered()
    service.bind_effect_applier(applier)
    served = service._cache.commit_projection

    async def commit_projection(authority, *args, **kwargs):
        events.append(("served", authority.access_id))
        return await served(authority, *args, **kwargs)

    service._cache.commit_projection = commit_projection
    members = _service_members(before, after)
    await _service_stage(service, members, effects=EFFECTS)
    lead_id = tx.member_transaction_id(GROUP, 0)
    assert (await tx.read_receipt(store, lead_id))["effects"] == EFFECTS
    assert "effects" not in await tx.read_receipt(store, tx.member_transaction_id(GROUP, 1))
    await _service_finish(store, service, "committed")
    assert applier.applied == [(lead_id, "grant_binding", "tok-1")]
    # No member is served AFTER before the group's effects are applied.
    assert events[0] == ("effect", "tok-1") and sorted(e for e in events[1:]) == sorted(
        ("served", member[2].access_id) for member in members)
    assert [await _read(store, member[2]) for member in members] == [member[2] for member in members]


@pytest.mark.asyncio
async def test_a_group_whose_second_member_moved_never_prepares_and_aborts_cleanly(tmp_path):
    store, service, before, after = await _setup(tmp_path)
    members = _service_members(before, after)
    # Another writer creates the "new" id first: its member can no longer stage as absent.
    created = next(member[2] for member in members if member[1] is None)
    await service.commit(created, subject_hash=SUBJECT_HASH, expected_revision=0, now=NOW)
    with pytest.raises(tx.CardTransactionRefused, match="card_transaction_absent_slot_used"):
        await _service_stage(service, members)
    group = await tx.read_receipt(store, GROUP)
    assert group["staged"] is False
    decided = await _service_finish(store, service, "aborted")
    assert decided["state"] == "aborted" and await tx.list_in_doubt(store) == []
    assert await _read(store, before) == before and await _read(store, created) == created
