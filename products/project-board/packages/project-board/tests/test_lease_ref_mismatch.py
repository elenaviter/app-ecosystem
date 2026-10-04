"""A mistyped message ref is named, never read as mailbox loss (W534).

W452 (2026-10-03): a session listed its lease, then lease-read reported the
mail absent and settle returned a raw storage error with a file path. The
supplied ref carried a 30-character id, a shape no mail has. The mail was
intact. When no record matches, lease-read and settle now name the held
lease's actual ref if this session holds the supplied lease, call a ref no
mail can have malformed, and report absence only otherwise.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from project_board.client.store import SharedFieldStore
from project_board.contract.errors import DomainError

PROJECT = "project-one"
WORKER = "claude-docs"
SESSION = "session-docs"


@pytest.fixture
def field(tmp_path: Path) -> SharedFieldStore:
    store = SharedFieldStore(tmp_path / "shared-field")
    store.initialize(field_id="test-field")
    for name in ("codex-api", WORKER):
        store.register_worker(
            worker_name=name,
            runtime_kind="codex",
            capabilities=[],
            authority_label=f"authority:{name}",
        )
    store.create_project(
        project_id=PROJECT, title="Lease refs", goal="Name a mistyped ref.", owner="operator"
    )
    return store


def _leased(field, key="one", owner=SESSION):
    sent = field.send_mail(
        PROJECT, sender="codex-api", recipient=WORKER, kind="request",
        subject="Read me", body="Then settle.", idempotency_key=f"send-{key}",
    )
    [leased] = field.pull_mail(PROJECT, worker_name=WORKER, lease_owner=owner)
    return sent["message_ref"], leased["lease"]["lease_id"]


def _with_id(message_ref: str, object_id: str) -> str:
    namespace, kind, stamp, _, semantic = message_ref.split(":")
    return ":".join([namespace, kind, stamp, object_id, semantic])


def _read(field, message_ref, lease_id, owner=SESSION):
    return field.read_worker_mail_lease(
        PROJECT, worker_name=WORKER, message_ref=message_ref,
        lease_id=lease_id, lease_owner=owner,
    )


def _settle(field, message_ref, lease_id, owner=SESSION):
    return field.settle_mail(
        PROJECT, worker_name=WORKER, message_ref=message_ref,
        lease_id=lease_id, lease_owner=owner, outcome="acknowledged",
    )


def _inventory(field, owner=SESSION):
    page = field.list_worker_mail_leases(WORKER, lease_owner=owner)
    return [(row["message_ref"], row["lease_id"]) for row in page["items"]]


def test_the_w452_shape_names_the_held_leases_actual_ref(field):
    # W452: the session held the lease and supplied its ref two characters short.
    message_ref, lease_id = _leased(field)
    shortened = _with_id(message_ref, message_ref.split(":")[3][:-2])

    for call in (_read, _settle):
        with pytest.raises(DomainError) as refused:
            call(field, shortened, lease_id)
        assert refused.value.code == "field_mail_lease_ref_mismatch"
        assert refused.value.details["actual_message_ref"] == message_ref
        assert "leased" not in str(refused.value)

    # Nothing moved: the lease is listed, readable and settles once.
    assert _inventory(field) == [(message_ref, lease_id)]
    assert _read(field, message_ref, lease_id)["message_ref"] == message_ref
    _settle(field, message_ref, lease_id)
    assert _inventory(field) == []


def test_a_ref_no_mail_can_have_is_named_malformed_not_absent(field):
    message_ref, lease_id = _leased(field)
    shortened = _with_id(message_ref, message_ref.split(":")[3][:-2])

    for call in (_read, _settle):
        with pytest.raises(DomainError) as refused:
            call(field, shortened, "lease_" + "0" * 32)
        assert refused.value.code == "field_mail_ref_malformed"
        assert refused.value.status == 400
        assert refused.value.details["message_ref"] == shortened
        assert "leased" not in str(refused.value)
    assert _inventory(field) == [(message_ref, lease_id)]


@pytest.mark.parametrize(
    "object_id",
    [
        "mail_" + "a" * 32,
        "mail-priority_20261004T091815897471Z_" + "b" * 32,
        "delivery_failed_" + "c" * 24,
    ],
)
def test_every_minted_id_shape_reads_as_absent_never_malformed(field, object_id):
    message_ref, _ = _leased(field)
    unknown = _with_id(message_ref, object_id)
    for call in (_read, _settle):
        with pytest.raises(DomainError) as refused:
            call(field, unknown, "lease_" + "0" * 32)
        assert refused.value.code == "field_mail_lease_not_found"
        assert refused.value.details["reason"] == "absent"


def test_a_held_lease_under_another_ref_names_the_actual_ref(field):
    message_ref, lease_id = _leased(field)
    wrong = _with_id(message_ref, "mail_" + "0" * 32)

    for call in (_read, _settle):
        with pytest.raises(DomainError) as refused:
            call(field, wrong, lease_id)
        assert refused.value.code == "field_mail_lease_ref_mismatch"
        assert refused.value.status == 409
        assert refused.value.details["actual_message_ref"] == message_ref
        assert refused.value.details["actual_project_ref"] == f"work:project:{PROJECT}"
        assert refused.value.details["message_ref"] == wrong
        assert "leased" not in str(refused.value)

    _settle(field, message_ref, lease_id)
    with pytest.raises(DomainError) as again:
        _settle(field, message_ref, lease_id)
    assert again.value.code == "field_mail_already_settled"


def test_another_sessions_lease_is_not_disclosed(field):
    message_ref, lease_id = _leased(field, owner="session-other")
    wrong = _with_id(message_ref, "mail_" + "0" * 32)

    for call in (_read, _settle):
        with pytest.raises(DomainError) as refused:
            call(field, wrong, lease_id)
        assert refused.value.code == "field_mail_lease_not_found"
        assert refused.value.details["reason"] == "absent"
        assert message_ref not in json.dumps(refused.value.details) + str(refused.value)


def test_an_expired_lease_is_not_disclosed(field):
    message_ref, lease_id = _leased(field)
    leased_path = (
        field._mail_root(PROJECT, WORKER) / "leased" / f"{message_ref.split(':')[3]}.json"
    )
    row = json.loads(leased_path.read_text())
    row["lease"]["expires_at"] = "2000-01-01T00:00:00Z"
    leased_path.write_text(json.dumps(row))
    wrong = _with_id(message_ref, "mail_" + "0" * 32)

    with pytest.raises(DomainError) as refused:
        _settle(field, wrong, lease_id)
    assert refused.value.code == "field_mail_lease_not_found"
    assert message_ref not in json.dumps(refused.value.details) + str(refused.value)


def test_a_genuinely_unknown_ref_and_lease_read_as_absent_without_a_path(field):
    message_ref, _ = _leased(field)
    unknown = _with_id(message_ref, "mail_" + "f" * 32)

    for call in (_read, _settle):
        with pytest.raises(DomainError) as refused:
            call(field, unknown, "lease_" + "0" * 32)
        assert refused.value.code == "field_mail_lease_not_found"
        assert refused.value.details["reason"] == "absent"
        assert "leased" not in str(refused.value)
        assert "/" not in str(refused.value)
