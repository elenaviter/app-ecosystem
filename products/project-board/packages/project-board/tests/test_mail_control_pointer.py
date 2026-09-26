"""A discard finds its message by one read, never by listing the mailbox (W287, LS3).

A discard names the control it withdraws. Without a message ref it used to list
and read every file in the mailbox's inbox, leased, processed and ignored
folders under the mailbox lock: on dev-main that was more than two thousand
processed rows for one agent while its receive waited. Delivery now writes a
pointer from the control's command ref to its message.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from project_board.client.store import SharedFieldStore


WORKER = "codex-api"
PROJECT = "project-one"
PROJECT_REF = f"work:project:{PROJECT}"


@pytest.fixture
def field(tmp_path: Path) -> SharedFieldStore:
    store = SharedFieldStore(tmp_path / "shared-field")
    store.initialize(field_id="test-field")
    store.register_worker(worker_name=WORKER, runtime_kind="codex", capabilities=[], authority_label="authority:codex-api")
    store.create_project(project_id=PROJECT, title="Pointers", goal="Discard by one read.", owner="operator")
    return store


def _deliver(field: SharedFieldStore, command: str, subject: str) -> dict:
    return field.send_mail(
        PROJECT,
        sender="control-plane",
        recipient=WORKER,
        kind="request",
        subject=subject,
        body=f"{subject}.",
        payload={"command_ref": f"work:control:{command}"},
        idempotency_key=f"control:work:control:{command}",
    )


def _discard(field: SharedFieldStore, command: str, **target) -> dict:
    result = field.discard_control_messages(
        worker_name=WORKER,
        discard_ref=f"work:control:discard-{command}-{len(target)}",
        sender_label="Operator",
        reason="The requirements changed.",
        targets=[{"command_ref": f"work:control:{command}", "project_ref": PROJECT_REF, **target}],
    )
    return result["outcomes"][0]


@pytest.fixture
def listed(monkeypatch) -> list[str]:
    """Every mailbox folder the code under test lists."""

    seen: list[str] = []
    original = Path.glob

    def recording(self: Path, pattern: str):
        seen.append(self.name)
        return original(self, pattern)

    monkeypatch.setattr(Path, "glob", recording)
    return seen


def test_delivery_writes_the_pointer_and_a_discard_reads_it_instead_of_listing(field, listed):
    pending = _deliver(field, "pending", "Pending task")
    received = _deliver(field, "received", "Received task")
    # Receive both, and settle the second so it sits in processed/.
    rows = field.pull_mail(PROJECT, worker_name=WORKER, lease_owner="session-one", limit=5)
    for row in rows:
        if row["message_ref"] != received["message_ref"]:
            continue
        field.settle_mail(
            PROJECT, worker_name=WORKER, message_ref=row["message_ref"],
            lease_id=row["lease"]["lease_id"], lease_owner="session-one", outcome="acknowledged",
        )

    pointer = field._mail_control_pointer(PROJECT, WORKER, "work:control:received")
    assert pointer.is_file()
    relative = pointer.relative_to(
        field._project_dir(PROJECT) / "mail-by-control" / WORKER
    )
    assert len(relative.parts) == 5
    assert all(part.isdigit() for part in relative.parts[:4])

    listed.clear()
    # One settled, one leased: both were received, and processed/ is never listed.
    assert _discard(field, "received")["outcome"] == "already_received"
    assert _discard(field, "pending")["outcome"] == "already_received"
    assert "processed" not in listed, listed


def test_a_pending_message_is_discarded_through_its_pointer(field, listed):
    _deliver(field, "fresh", "Fresh task")
    listed.clear()

    assert _discard(field, "fresh")["outcome"] == "discarded_before_receipt"
    assert "processed" not in listed
    assert field.pull_mail(PROJECT, worker_name=WORKER, lease_owner="session-one") == []


def test_a_control_delivered_before_the_pointer_lists_only_the_states_in_flight(field, listed):
    _deliver(field, "legacy-pending", "Legacy pending")
    done = _deliver(field, "legacy-done", "Legacy done")
    for row in field.pull_mail(PROJECT, worker_name=WORKER, lease_owner="session-one", limit=5):
        if row["message_ref"] == done["message_ref"]:
            field.settle_mail(
                PROJECT, worker_name=WORKER, message_ref=row["message_ref"],
                lease_id=row["lease"]["lease_id"], lease_owner="session-one", outcome="acknowledged",
            )
    for command in ("legacy-pending", "legacy-done"):
        field._mail_control_pointer(PROJECT, WORKER, f"work:control:{command}").unlink()
    listed.clear()

    assert _discard(field, "legacy-pending")["outcome"] == "already_received"
    # A processed message is not searched for: the control plane's hint says it was received.
    assert _discard(field, "legacy-done", received_hint=True)["outcome"] == "already_received"
    assert _discard(field, "legacy-done")["outcome"] == "not_present"
    assert "processed" not in listed
    assert {"inbox", "leased"} <= set(listed)


def test_retention_bounds_the_pointers_and_project_less_processed_mail(field):
    _deliver(field, "any", "Any task")
    field._mail_history().write(
        project_id="",
        family="mail-processed",
        agent=WORKER,
        record_id="mail_direct",
        row={"message_id": "mail_direct", "recipient": WORKER, "created_at": "2026-09-25T00:00:00Z"},
    )

    stores = {store.store: store.root for store in field._mail_history().stores()}

    assert stores["mail-by-control"] == field._project_dir(PROJECT) / "mail-by-control"
    assert stores["mail-processed"] == field.control / "unscoped" / "mail-processed"
