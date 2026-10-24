"""Completed CardVersion Saves must not strand their next policy change."""

from dataclasses import replace
from datetime import timedelta

import pytest

from connection_hub.delegated_credentials.cards import transaction_store as tx
from connection_hub.delegated_credentials.cards.card_version_port import _port_effect
from connection_hub.delegated_credentials.cards.policy_marker_settlement import CardPolicyMarkerSettler
from connection_hub.invocation_policy import InvocationPolicyConflict, POLICY_ALWAYS
from connection_hub.invocation_policy.store import owner_hash_for
import test_w756_card_version_policies as real
from test_w661_card_versions import DIGEST, SUBJECT_HASH, WHEN

TXN = "pb-save-" + "a" * 64


async def _saved(tmp_path, monkeypatch, *, publish=True):
    monkeypatch.setattr(real, "TXN", TXN)
    store, service, before, after, policies, operation, effects, prepare = await real.setup(tmp_path)
    await service.stage_card_version(
        txn=TXN, request_digest=DIGEST, catalog="catalog-1", now=WHEN,
        members=[(SUBJECT_HASH, before.access_id, 1, after)], effects=effects, prepare=prepare,
        binding={"caller": "example-caller", "scope": "project:example"},
    )
    if publish:
        await service.publish_card_version(
            txn=TXN, run_effect=_port_effect(operation._apply),
            binding={"caller": "example-caller", "scope": "project:example"},
        )
    policies.bind_transaction_settler(CardPolicyMarkerSettler(
        card_store=store, coordinator=None, callers={}, max_wait_seconds=0.1,
    ))
    authority = operation._policy_authority(effects[0])
    return store, service, before, after, policies, authority


@pytest.mark.asyncio
@pytest.mark.parametrize("next_change", ["set", "prepare"])
async def test_card_save_then_next_policy_change(tmp_path, monkeypatch, next_change):
    store, _service, before, _after, policies, authority = await _saved(tmp_path, monkeypatch)
    assert await tx.read_card_version_marker(store, TXN) is None
    assert (await policies.get_policy_change(owner_subject=before.grantor_subject, authority=authority)).state == "committed"
    if next_change == "set":
        result = await policies.set_policy(owner_subject=before.grantor_subject, authority=authority,
                                           mode=POLICY_ALWAYS, expected_revision=1)
        assert result.revision == 2
    else:
        result = await policies.prepare_policy_change(owner_subject=before.grantor_subject, authority=authority,
            mode=POLICY_ALWAYS, expected_revision=1, change_id="next-save")
        assert result.change_id == "next-save"


@pytest.mark.asyncio
@pytest.mark.parametrize("successors", [1, 64, 65])
async def test_save_save_again_then_switch_and_proof_bound(tmp_path, monkeypatch, successors):
    store, service, before, after, policies, authority = await _saved(tmp_path, monkeypatch)
    current = after
    for index in range(successors):
        txn = "successor-" + f"{index:064x}"
        following = replace(current, card_revision=current.card_revision + 1, label=f"successor {index}")
        await service.stage_card_version(
            txn=txn, request_digest=DIGEST, catalog="catalog-1", now=WHEN + timedelta(seconds=index + 1),
            members=[(SUBJECT_HASH, before.access_id, current.card_revision, following)],
        )
        await service.publish_card_version(txn=txn)
        current = following
    if successors == 65:
        with pytest.raises(InvocationPolicyConflict, match="invocation_policy_earlier_change_undecided"):
            await policies.set_policy(owner_subject=before.grantor_subject, authority=authority, mode=POLICY_ALWAYS)
        assert await policies.get_policy_change(owner_subject=before.grantor_subject, authority=authority) is not None
    else:
        saved = await policies.set_policy(owner_subject=before.grantor_subject, authority=authority,
                                          mode=POLICY_ALWAYS, expected_revision=1)
        assert saved.revision == 2
    assert (await store.read_current(subject_hash=SUBJECT_HASH, access_id=before.access_id)).card_revision == current.card_revision


@pytest.mark.asyncio
@pytest.mark.parametrize("distance", [1, 64, 65])
async def test_historical_recordless_writer_uses_bounded_card_local_recovery(tmp_path, monkeypatch, distance):
    store, _service, before, after, policies, authority = await _saved(tmp_path, monkeypatch)
    # Simulate the old direct issuer, not CardVersion: no version_record.
    pointer = await store.write_revision(
        subject_hash=SUBJECT_HASH,
        authority=replace(after, card_revision=after.card_revision + distance, last_issued_at=123),
        updated_at=WHEN + timedelta(seconds=1),
    )
    await store.advance_current(subject_hash=SUBJECT_HASH, pointer=pointer)
    if distance <= 64:
        assert (await policies.set_policy(owner_subject=before.grantor_subject, authority=authority,
                                          mode=POLICY_ALWAYS, expected_revision=1)).revision == 2
    else:
        with pytest.raises(InvocationPolicyConflict, match="invocation_policy_earlier_change_undecided"):
            await policies.set_policy(owner_subject=before.grantor_subject, authority=authority, mode=POLICY_ALWAYS)
    assert await store.read_current(subject_hash=SUBJECT_HASH, access_id=before.access_id) == pointer


@pytest.mark.asyncio
async def test_history_inventory_refuses_its_directory_entry_cap(tmp_path, monkeypatch):
    store, _service, before, after, policies, authority = await _saved(tmp_path, monkeypatch)
    pointer = await store.write_revision(subject_hash=SUBJECT_HASH,
        authority=replace(after, card_revision=3), updated_at=WHEN + timedelta(seconds=1))
    await store.advance_current(subject_hash=SUBJECT_HASH, pointer=pointer)
    directory = store.revision_path(subject_hash=SUBJECT_HASH, access_id=before.access_id,
                                    revision_name=pointer.revision_name).parent
    # Even non-version entries count: do not sort or scan an unbounded directory.
    from connection_hub.delegated_credentials.cards.policy_marker_settlement import CARD_VERSION_HISTORY_NAMES
    for index in range(CARD_VERSION_HISTORY_NAMES + 1):
        (directory / f"unrelated-{index}").touch()
    marker = await policies.get_policy_change(owner_subject=before.grantor_subject, authority=authority)
    with pytest.raises(InvocationPolicyConflict, match="invocation_policy_earlier_change_undecided"):
        await policies.set_policy(owner_subject=before.grantor_subject, authority=authority, mode=POLICY_ALWAYS)
    assert await policies.get_policy_change(owner_subject=before.grantor_subject, authority=authority) == marker


@pytest.mark.asyncio
@pytest.mark.parametrize("mixed", [False, True])
async def test_oauth_issuance_persistence_keeps_its_own_predecessor_record(tmp_path, monkeypatch, mixed):
    from connection_hub.delegated_credentials.automation_access import AutomationAccessService, record_from_card
    from types import SimpleNamespace

    store, service, before, after, policies, authority = await _saved(tmp_path, monkeypatch)
    host = object.__new__(AutomationAccessService)
    host._caller_writers = None
    host._card_coordinator = None

    async def persist(card, _handles, *, subject_hash, expected_revision):
        await service.commit(card, subject_hash=subject_hash, expected_revision=expected_revision,
                             now=int(WHEN.timestamp()))

    host._persistence = SimpleNamespace(persist=persist)

    async def notify(*_args, **_kwargs):
        pass

    monkeypatch.setattr(host, "notify_change", notify)
    current = after
    for index in range(2 if mixed else 1):
        base = await store.read_current(subject_hash=SUBJECT_HASH, access_id=before.access_id)
        renewed = replace(current, card_revision=current.card_revision + 1,
                          expires_at=current.expires_at + 1, last_issued_at=123 + index)
        record = record_from_card(renewed)

        async def build(**_kwargs):
            # Candidate construction is isolated; record_oauth_grant,
            # _persist_record and the real Card service writer are not.
            return record, current.card_revision, False, {}

        monkeypatch.setattr(host, "_oauth_grant_record", build)
        assert await host.record_oauth_grant(grantor_subject=current.grantor_subject,
                                             client_id=current.client_id) == record
        pointer = await store.read_current(subject_hash=SUBJECT_HASH, access_id=before.access_id)
        own = await store.read_version_record(subject_hash=SUBJECT_HASH, access_id=before.access_id,
                                               revision_name=pointer.revision_name)
        assert own["base"] == base.revision_name
        assert own["txn"] == "" and own["txn"] != TXN
        assert own["at"] and own["actor"] == {"kind": "card-service"}
        current = renewed
    if mixed:
        following = replace(current, card_revision=current.card_revision + 1, label="another board save")
        txn = "another-board-save-" + "c" * 32
        await service.stage_card_version(txn=txn, request_digest=DIGEST, catalog="catalog-1",
            now=WHEN + timedelta(seconds=3),
            members=[(SUBJECT_HASH, before.access_id, current.card_revision, following)])
        await service.publish_card_version(txn=txn)

    async def no_inventory(**_kwargs):
        raise AssertionError("new writers must keep the exact predecessor chain")

    monkeypatch.setattr(store, "policy_recovery_candidates", no_inventory)
    assert (await policies.set_policy(owner_subject=before.grantor_subject, authority=authority,
                                      mode=POLICY_ALWAYS, expected_revision=1)).revision == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["prepared", "wrong_txn", "wrong_owner", "wrong_access", "wrong_revision",
                                 "missing_record", "wrong_at", "wrong_binding", "pending_txn"])
async def test_unproved_card_version_marker_stays_refused(tmp_path, monkeypatch, bad):
    store, _service, before, _after, policies, authority = await _saved(tmp_path, monkeypatch)
    owner = before.grantor_subject
    marker = await policies.get_policy_change(owner_subject=owner, authority=authority)
    if bad in {"prepared", "wrong_txn", "wrong_owner", "wrong_access", "wrong_revision"}:
        if bad == "prepared":
            marker = replace(marker, state="prepared", committed_at=0, policy_revision=0)
        elif bad == "wrong_txn":
            marker = replace(marker, change_id=f"pb-save-{'b' * 64}:{authority.key}")
        elif bad == "wrong_owner":
            owner = "another-owner"
        elif bad == "wrong_access":
            authority = replace(authority, access_id="another-card")
            marker = replace(marker, authority=authority, change_id=f"{TXN}:{authority.key}")
        else:
            marker = replace(marker, expected_policy_revision=1, policy_revision=2)
        await policies._store.write_policy_change(owner_hash=owner_hash_for(owner), change=marker)
    elif bad == "pending_txn":
        await store.write_marker_document(TXN, {
            "schema": tx.CARD_VERSION_MARKER_SCHEMA, "txn": TXN, "state": "published", "members": [],
            "effects": [{"kind": "invocation_policy"}], "effect_outcomes": {},
        })
    else:
        read_record = store.read_version_record

        async def changed_record(**kwargs):
            record = await read_record(**kwargs)
            if bad == "missing_record":
                return None
            return {**record, "at": "2000-01-01T00:00:00+00:00"} if bad == "wrong_at" else {
                **record, "binding": {"caller": "other", "scope": "other"}}

        monkeypatch.setattr(store, "read_version_record", changed_record)
    with pytest.raises(InvocationPolicyConflict, match="invocation_policy_earlier_change_undecided"):
        await policies.set_policy(owner_subject=owner, authority=authority, mode=POLICY_ALWAYS)
    assert await policies.get_policy_change(owner_subject=owner, authority=authority) == marker


@pytest.mark.asyncio
async def test_a_long_lived_card_past_256_entries_still_settles_across_a_recordless_renewal(tmp_path, monkeypatch):
    """24 Oct live case: an agent Card with 301 revisions whose chain a record-less renewal broke. The old
    256-entry scan refused (policy_recovery_history_limit), so every re-add of a permission it had saved stayed
    invocation_policy_earlier_change_undecided. The scan now admits a long-lived Card; the 64-revision window,
    the ambiguity refusal and every publication check are unchanged."""
    store, _service, before, after, policies, authority = await _saved(tmp_path, monkeypatch)
    pointer = await store.write_revision(  # the renewal the old coordinated writer stored without a record
        subject_hash=SUBJECT_HASH, authority=replace(after, card_revision=after.card_revision + 1, last_issued_at=123),
        updated_at=WHEN + timedelta(seconds=1))
    await store.advance_current(subject_hash=SUBJECT_HASH, pointer=pointer)
    directory = store.revision_path(subject_hash=SUBJECT_HASH, access_id=before.access_id,
                                    revision_name=pointer.revision_name).parent
    for index in range(300):  # older history of a Card renewed every hour
        (directory / f"older-{index}").touch()
    assert (await policies.set_policy(owner_subject=before.grantor_subject, authority=authority,
                                      mode=POLICY_ALWAYS, expected_revision=1)).revision == 2
    assert await policies.get_policy_change(owner_subject=before.grantor_subject, authority=authority) is None


@pytest.mark.asyncio
async def test_a_coordinated_card_transaction_records_its_predecessor(tmp_path):
    """The writer that stored the 24 Oct renewal without a record now links its revision to the one it
    replaced, so the history stays walkable by name and no inventory is needed."""
    from test_card_service import SUBJECT_HASH as TX_SUBJECT
    from test_card_transaction_store import _setup, _stage

    store, _service, before_card, after_card = await _setup(tmp_path)
    replaced = await store.read_current(subject_hash=TX_SUBJECT, access_id=before_card.access_id)
    receipt = await _stage(store, before_card, after_card)
    record = await store.read_version_record(subject_hash=TX_SUBJECT, access_id=before_card.access_id,
                                             revision_name=receipt["after"]["revision_name"])
    assert record["base"] == replaced.revision_name == receipt["before"]["revision_name"]
    assert record["txn"] == "" and record["actor"] == {"kind": "card-transaction"} and record["at"]


def test_a_predecessor_record_names_only_the_replaced_revision():
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from connection_hub.delegated_credentials.cards.store import predecessor_record

    at = datetime(2026, 10, 24, 22, 48, 35, tzinfo=timezone.utc)
    assert predecessor_record({"revision_name": "r-1"}, at=at, actor_kind="issuer-update") == {
        "txn": "", "base": "r-1", "at": at.isoformat(), "actor": {"kind": "issuer-update"}}
    assert predecessor_record(SimpleNamespace(revision_name="r-2"), at=at, actor_kind="x")["base"] == "r-2"
    assert predecessor_record(None, at=at, actor_kind="x")["base"] is None
