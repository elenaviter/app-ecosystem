"""Within-shard receive uses stored creation time, not random file identity."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from project_board.client import store as store_module
from project_board.client.io import atomic_write_json, content_hash, read_json
from project_board.client.session import pull_worker_input
from project_board.client.store import SharedFieldStore
from project_board.contract.errors import DomainError


WORKER = "codex-receiver"
PEER = "codex-sender"
PROJECTS = ("order-one", "order-two")


@pytest.fixture
def field(tmp_path: Path, monkeypatch) -> SharedFieldStore:
    store = SharedFieldStore(tmp_path / "field")
    store.initialize(field_id="order-test")
    for worker in (WORKER, PEER):
        store.register_worker(
            worker_name=worker, runtime_kind="codex", capabilities=[],
            authority_label=f"authority:{worker}",
        )
        store.listen_worker(worker)
    for project in PROJECTS:
        store.create_project(
            project_id=project, title=project, goal="Ordered mail.", owner="operator",
        )
    store.sync_worker_attendances(WORKER, [f"work:project:{p}" for p in PROJECTS])
    original = store_module.new_id
    ids = iter(f"mail_{number:032x}" for number in range(255, 0, -1))
    monkeypatch.setattr(
        store_module, "new_id",
        lambda prefix: next(ids) if prefix == "mail" else original(prefix),
    )
    return store


def _send(field, monkeypatch, key, at, *, project=PROJECTS[0], correlation=""):
    with monkeypatch.context() as clock:
        clock.setattr(store_module, "utc_now", lambda: at)
        sent = field.send_mail(
            project, sender=PEER, recipient=WORKER, kind="reply", subject=key,
            body=f"Complete body for {key}: ✓", payload={"evidence": key},
            correlation_id=correlation, idempotency_key=key,
            board_routed=not bool(project),
        )
    return field._mail_record_unlocked(project, WORKER, sent["message_ref"])


def _refs(result):
    return [row["message_ref"] for row in result]


def _pull(field, *, project=PROJECTS[0], limit=10):
    return field.pull_mail(project, worker_name=WORKER, lease_owner=WORKER, limit=limit)


def _operator(field, *, project=PROJECTS[0]):
    payload = {"body": "Handle the operator first.", "correlation_id": "operator-thread"}
    return field.materialize_control({
        "ref": f"work:control:order-{project}", "project_ref": f"work:project:{project}",
        "recipient": WORKER, "kind": "request", "subject": "Operator input",
        "payload": payload, "payload_hash": content_hash(payload),
        "sender_identity": {"kind": "user", "label": "Operator"},
    })


def test_older_peer_precedes_newer_peer_with_inverted_random_ids(field, monkeypatch):
    oldest = _send(field, monkeypatch, "oldest", "2026-10-01T08:00:00Z")
    newest = _send(field, monkeypatch, "newest", "2026-10-02T08:00:00Z")
    assert oldest["message_id"] > newest["message_id"]
    first, second = _pull(field, limit=1), _pull(field, limit=1)
    assert _refs(first + second) == [oldest["message_ref"], newest["message_ref"]]
    assert _pull(field) == []


def test_equal_instants_use_stable_message_identity_not_timestamp_spelling(field, monkeypatch):
    first = _send(field, monkeypatch, "utc", "2026-10-03T10:00:00Z")
    second = _send(field, monkeypatch, "offset", "2026-10-03T12:00:00+02:00")
    rows = _pull(field)
    assert _refs(rows) == [second["message_ref"], first["message_ref"]]
    assert [row["created_at"] for row in rows] == [second["created_at"], first["created_at"]]


def test_offset_timestamp_is_compared_as_an_instant_not_lexical_text(field, monkeypatch):
    earlier = _send(field, monkeypatch, "earlier", "2026-10-03T11:00:00+02:00")
    later = _send(field, monkeypatch, "later", "2026-10-03T10:00:00Z")
    assert _refs(_pull(field)) == [earlier["message_ref"], later["message_ref"]]


@pytest.mark.parametrize("timestamp", [None, "", "not-a-timestamp"])
def test_unknown_timestamp_is_retained_after_valid_peers_without_repair(field, monkeypatch, timestamp):
    valid = _send(field, monkeypatch, "valid", "2026-10-03T10:00:00Z")
    unknown = _send(field, monkeypatch, "unknown", "2026-10-03T11:00:00Z")
    path = field._mail_root(PROJECTS[0], WORKER) / "inbox" / f"{unknown['message_id']}.json"
    row = read_json(path)
    if timestamp is None:
        row.pop("created_at")
    else:
        row["created_at"] = timestamp
    atomic_write_json(path, row)
    received = _pull(field)
    assert _refs(received) == [valid["message_ref"], unknown["message_ref"]]
    assert received[1].get("created_at") == timestamp
    assert received[1]["body"] == unknown["body"]


def test_unknown_timestamp_ties_are_stable_identity_order(field, monkeypatch):
    messages = [_send(field, monkeypatch, key, "2026-10-03T10:00:00Z") for key in ("a", "b", "c")]
    for message in messages:
        path = field._mail_root(PROJECTS[0], WORKER) / "inbox" / f"{message['message_id']}.json"
        row = read_json(path)
        row["created_at"] = "invalid"
        atomic_write_json(path, row)
    assert _refs(_pull(field)) == [row["message_ref"] for row in reversed(messages)]


def test_legacy_operator_admission_does_not_depend_on_priority_filename(field, monkeypatch):
    older = _send(field, monkeypatch, "older-peer", "2026-10-01T08:00:00Z")
    operator = _operator(field)
    root = field._mail_root(PROJECTS[0], WORKER) / "inbox"
    path = root / f"{operator['message_id']}.json"
    row = read_json(path)
    row.pop("admitted_operator_control")  # older clients retain the trusted control ref
    row["created_at"] = "invalid"
    atomic_write_json(path, row)
    path.rename(root / "zz-legacy-operator.json")
    assert _refs(_pull(field)) == [operator["message_ref"], older["message_ref"]]


def test_correlated_receive_orders_selected_peers_and_cannot_bypass_operator(field, monkeypatch):
    oldest = _send(field, monkeypatch, "oldest", "2026-10-01T08:00:00Z", correlation="thread")
    newest = _send(field, monkeypatch, "newest", "2026-10-02T08:00:00Z", correlation="thread")
    operator = _operator(field, project=PROJECTS[1])
    blocked = pull_worker_input(field, worker_name=WORKER, correlation_id="thread", sender=PEER)
    assert blocked["items"] == []
    assert blocked["selection"]["state"] == "operator_pending"
    ordinary = pull_worker_input(field, worker_name=WORKER)
    assert operator["message_ref"] in _refs([item["message"] for item in ordinary["items"]])
    # Return only our fixture peers, then exercise the selected-path claim itself.
    for item in ordinary["items"]:
        message = item["message"]
        if message["message_ref"] in {oldest["message_ref"], newest["message_ref"]}:
            field.rollback_mail_lease(
                PROJECTS[0], worker_name=WORKER, message_ref=message["message_ref"],
                lease_id=message["lease"]["lease_id"], lease_owner=WORKER,
            )
    selected = pull_worker_input(field, worker_name=WORKER, correlation_id="thread", sender=PEER, limit=1)
    assert _refs([item["message"] for item in selected["items"]]) == [oldest["message_ref"]]
    continuation = pull_worker_input(field, worker_name=WORKER)
    assert _refs([item["message"] for item in continuation["items"]]) == [newest["message_ref"]]


def test_bounded_receive_preserves_full_messages_leases_and_single_settlement(field, monkeypatch):
    messages = [_send(field, monkeypatch, f"mail-{i}", f"2026-10-03T10:00:0{i}Z") for i in range(4)]
    first = pull_worker_input(field, worker_name=WORKER, limit=2)
    second = pull_worker_input(field, worker_name=WORKER, limit=2)
    received = [item["message"] for batch in (first, second) for item in batch["items"]]
    assert _refs(received) == _refs(messages)
    assert first["delivery"]["remaining_count"] == 2
    assert first["delivery"]["has_more"] is True
    assert second["delivery"]["remaining_count"] == 0
    assert len({row["lease"]["lease_id"] for row in received}) == 4
    for expected, actual in zip(messages, received):
        for key in ("message_id", "message_ref", "correlation_id", "idempotency_key", "body", "payload"):
            assert actual[key] == expected[key]
        stored = field._mail_record_unlocked(PROJECTS[0], WORKER, actual["message_ref"])
        assert stored["lease"] == actual["lease"]
        reread = field.read_worker_mail_lease(
            PROJECTS[0], worker_name=WORKER, message_ref=actual["message_ref"],
            lease_id=actual["lease"]["lease_id"], lease_owner=WORKER,
        )
        assert reread["body"] == actual["body"]
        with pytest.raises(DomainError):
            field.read_worker_mail_lease(
                PROJECTS[0], worker_name=WORKER, message_ref=actual["message_ref"],
                lease_id=actual["lease"]["lease_id"], lease_owner="other-session",
            )
        settle = dict(
            worker_name=WORKER, message_ref=actual["message_ref"],
            lease_id=actual["lease"]["lease_id"], lease_owner=WORKER,
            outcome="acknowledged", summary="Handled once.",
        )
        field.settle_mail(PROJECTS[0], **settle)
        with pytest.raises(DomainError):
            field.settle_mail(PROJECTS[0], **settle)
    assert _pull(field) == []


def test_existing_direct_then_project_shard_order_is_not_global_fifo(field, monkeypatch):
    project_two = _send(field, monkeypatch, "project-two", "2026-10-01T08:00:00Z", project=PROJECTS[1])
    project_one = _send(field, monkeypatch, "project-one", "2026-10-02T08:00:00Z")
    direct = _send(field, monkeypatch, "direct", "2026-10-03T08:00:00Z", project="")
    result = pull_worker_input(field, worker_name=WORKER)
    assert _refs([item["message"] for item in result["items"]]) == [
        direct["message_ref"], project_one["message_ref"], project_two["message_ref"],
    ]
    assert [item["scope"] for item in result["items"]] == ["direct", "project", "project"]


def test_ordering_reads_each_pending_row_once_before_claim(field, monkeypatch):
    messages = [_send(field, monkeypatch, f"mail-{i}", f"2026-10-03T10:00:0{i}Z") for i in range(4)]
    reads = Counter()
    original = store_module.read_json

    def counted(path, *args, **kwargs):
        if path.parent.name == "inbox":
            reads[path.name] += 1
        return original(path, *args, **kwargs)

    monkeypatch.setattr(store_module, "read_json", counted)
    assert _refs(_pull(field, limit=2)) == _refs(messages[:2])
    assert reads == Counter({f"{message['message_id']}.json": 1 for message in messages})


def test_zero_capacity_reads_no_pending_bodies(field, monkeypatch):
    message = _send(field, monkeypatch, "pending", "2026-10-03T10:00:00Z")
    original = store_module.read_json

    def no_inbox_read(path, *args, **kwargs):
        assert path.parent.name != "inbox"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(store_module, "read_json", no_inbox_read)
    assert _pull(field, limit=0) == []
    monkeypatch.setattr(store_module, "read_json", original)
    assert _refs(_pull(field)) == [message["message_ref"]]


def test_failed_ordered_batch_rolls_back_all_claims_then_retries_in_order(field, monkeypatch):
    messages = [_send(field, monkeypatch, f"mail-{i}", f"2026-10-03T10:00:0{i}Z") for i in range(3)]
    original = store_module.atomic_write_json
    writes = 0

    def fail_second_claim(path, row):
        nonlocal writes
        if path.parent.name == "leased":
            writes += 1
            if writes == 2:
                raise RuntimeError("Fixture failure while writing the second lease")
        return original(path, row)

    with monkeypatch.context() as failure:
        failure.setattr(store_module, "atomic_write_json", fail_second_claim)
        with pytest.raises(RuntimeError, match="second lease"):
            _pull(field)
    root = field._mail_root(PROJECTS[0], WORKER)
    assert list((root / "leased").glob("*.json")) == []
    assert len(list((root / "inbox").glob("*.json"))) == 3
    assert _refs(_pull(field)) == _refs(messages)


def test_expired_redelivery_keeps_creation_order_and_fences_the_old_lease(field, monkeypatch):
    older = _send(field, monkeypatch, "older", "2026-10-01T10:00:00Z")
    newer = _send(field, monkeypatch, "newer", "2026-10-02T10:00:00Z")
    [first] = _pull(field, limit=1)
    assert first["message_ref"] == older["message_ref"]
    path = field._mail_root(PROJECTS[0], WORKER) / "leased" / f"{older['message_id']}.json"
    row = read_json(path)
    row["lease"]["expires_at"] = "2000-01-01T00:00:00Z"
    atomic_write_json(path, row)
    redelivered, following = _pull(field)
    assert _refs([redelivered, following]) == [older["message_ref"], newer["message_ref"]]
    assert redelivered["created_at"] == older["created_at"]
    assert redelivered["lease"]["lease_id"] != first["lease"]["lease_id"]
    with pytest.raises(DomainError):
        field.settle_mail(
            PROJECTS[0], worker_name=WORKER, message_ref=older["message_ref"],
            lease_id=first["lease"]["lease_id"], lease_owner=WORKER,
            outcome="acknowledged",
        )
    field.settle_mail(
        PROJECTS[0], worker_name=WORKER, message_ref=older["message_ref"],
        lease_id=redelivered["lease"]["lease_id"], lease_owner=WORKER,
        outcome="acknowledged",
    )
