"""The relay's wake never waits for a worker's mailbox lock (W448).

On dev-main, 2026-10-04 19:37-21:47Z, the relay's executor attribution named
``SharedFieldStore.pending_worker_mail_refs`` as the holder in 113 of 114 slow
turns, up to 31.1 s, all on the coordinator's channel. The wake decision read
the mailbox under its exclusive ``.mail.lock``, the same lock the worker's own
receive, settle and renew hold while they scan a 373-message inbox, so the
channel's heartbeat and assignment reconcile queued behind it.

The wake now reads with ``wait=False``: a busy lock is read lock-free, expired
leases are left for the holder or the next free read to recover, and a busy
lock held for a while is logged with its holder.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

import pytest

from project_board.client import io as io_module
from project_board.client import store as store_module
from project_board.client.io import exclusive_lock, read_lock_holder
from project_board.client.session import pull_worker_input
from project_board.client.store import SharedFieldStore

PROJECT = "project-one"
PROJECT_REF = f"work:project:{PROJECT}"
WORKER_SESSION = "11111111-1111-4111-8111-111111111111"
SENDER_SESSION = "22222222-2222-4222-8222-222222222222"
WORKER = f"codex-{WORKER_SESSION}"
SENDER = f"codex-{SENDER_SESSION}"
BUSY_LIMIT_SECONDS = 0.5


@pytest.fixture
def field(tmp_path: Path) -> SharedFieldStore:
    store = SharedFieldStore(tmp_path / "shared-field")
    store.initialize(field_id="w448-field")
    for name, session in ((WORKER, WORKER_SESSION), (SENDER, SENDER_SESSION)):
        store.register_worker(
            worker_name=name, runtime_kind="codex", runtime_session_id=session,
            capabilities=[], authority_label=f"authority:{session}",
        )
        store.listen_worker(name)
    store.create_project(project_id=PROJECT, title="Mail", goal="Wake on time.", owner="operator")
    store.sync_worker_attendances(WORKER, [PROJECT_REF])
    store.sync_worker_attendances(SENDER, [PROJECT_REF])
    return store


def _send(field: SharedFieldStore, number: int) -> str:
    sent = field.send_mail(
        PROJECT, sender=SENDER, recipient=WORKER, kind="request",
        subject=f"Task {number}", body="Do it.", idempotency_key=f"w448-task-{number}",
    )
    return str(sent.get("message_ref") or sent["message"]["message_ref"])


def _expire_one_lease(field: SharedFieldStore) -> str:
    """Lease one message and let the lease expire; return its ref."""

    [leased] = field.pull_mail(PROJECT, worker_name=WORKER, lease_owner="test", limit=1)
    path = field._mail_root(PROJECT, WORKER) / "leased" / f"{leased['message_ref'].split(':')[3]}.json"
    row = json.loads(path.read_text(encoding="utf-8"))
    row["lease"]["expires_at"] = "2000-01-01T00:00:00Z"
    path.write_text(json.dumps(row), encoding="utf-8")
    return leased["message_ref"]


class HeldMailLock:
    """Another holder of the worker's mailbox lock: a pb command in its own scan."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.held = threading.Event()
        self.release = threading.Event()
        self.thread = threading.Thread(target=self._hold, daemon=True)

    def _hold(self) -> None:
        with exclusive_lock(self.path):
            self.held.set()
            self.release.wait(10)

    def __enter__(self) -> "HeldMailLock":
        self.thread.start()
        assert self.held.wait(2), "the other holder never took the lock"
        return self

    def __exit__(self, *_exc) -> None:
        self.release.set()
        self.thread.join(2)


def _leased_files(field: SharedFieldStore) -> list[str]:
    return sorted(path.name for path in (field._mail_root(PROJECT, WORKER) / "leased").glob("*.json"))


def test_a_busy_mailbox_lock_never_holds_the_wake_read_and_still_sees_all_pending_mail(field):
    _send(field, 1)
    expired = _expire_one_lease(field)
    waiting = _send(field, 2)

    with HeldMailLock(field._mail_root(PROJECT, WORKER) / ".mail.lock"):
        started = time.monotonic()
        refs = field.pending_worker_mail_refs(WORKER, wait=False)
        seconds = time.monotonic() - started
        assert _leased_files(field), "a busy lock leaves recovery to its holder"

    assert seconds < BUSY_LIMIT_SECONDS, f"the wake read waited {seconds:.2f}s for a busy mailbox lock"
    assert set(refs) == {expired, waiting}, "inbox mail and the expired lease both count as pending"


def test_an_expired_lease_busy_for_several_reads_is_recovered_on_the_first_free_one_and_delivered_once(field):
    _send(field, 1)
    expired = _expire_one_lease(field)

    with HeldMailLock(field._mail_root(PROJECT, WORKER) / ".mail.lock"):
        for _tick in range(3):
            assert expired in field.pending_worker_mail_refs(WORKER, wait=False)
        assert _leased_files(field), "nothing is recovered while another holder has the lock"

    assert field.pending_worker_mail_refs(WORKER, wait=False) == [expired]
    assert _leased_files(field) == [], "the first free read recovers the expired lease"

    first = pull_worker_input(field, worker_name=WORKER, limit=5, lease_seconds=600)
    second = pull_worker_input(field, worker_name=WORKER, limit=5, lease_seconds=600)
    delivered = [item["message"]["message_ref"] for item in first["items"] + second["items"]]
    assert delivered == [expired], "the recovered message is delivered exactly once"


def test_a_mail_file_that_vanishes_between_listing_and_reading_is_skipped(field, monkeypatch):
    gone = _send(field, 1)
    kept = _send(field, 2)
    real_read_json = store_module.read_json
    removed: list[str] = []

    def read_json(path, *args, **kwargs):
        path = Path(path)
        if path.parent.name == "inbox" and not removed and gone.split(":")[3] in path.name:
            path.unlink()  # receive renamed it to leased/ between the listing and this read
            removed.append(path.name)
        return real_read_json(path, *args, **kwargs)

    monkeypatch.setattr(store_module, "read_json", read_json)
    with HeldMailLock(field._mail_root(PROJECT, WORKER) / ".mail.lock"):
        refs = field.pending_worker_mail_refs(WORKER, wait=False)

    assert removed, "the race was exercised"
    assert refs == [kept]


def test_a_busy_mailbox_lock_names_its_holder_and_how_long_it_has_held(field, monkeypatch, caplog):
    _send(field, 1)
    lock_path = field._mail_root(PROJECT, WORKER) / ".mail.lock"
    monkeypatch.setattr(store_module, "SLOW_LOCK_SECONDS", 0.0)

    with HeldMailLock(lock_path), caplog.at_level(logging.INFO, logger="project_board.client.locks"):
        holder = read_lock_holder(lock_path)
        field.pending_worker_mail_refs(WORKER, wait=False)

    assert holder["holder"].endswith("HeldMailLock._hold")
    assert holder["pid"] and holder["acquired_at"]
    busy = [record.getMessage() for record in caplog.records if "mail lock busy" in record.getMessage()]
    assert busy and "HeldMailLock._hold" in busy[0], busy
    assert read_lock_holder(lock_path) == {}, "a released lock names no holder"


def test_a_slow_lock_wait_and_hold_are_logged_once_with_their_holder(tmp_path, monkeypatch, caplog):
    lock_path = tmp_path / "locks" / "example.lock"
    monkeypatch.setattr(io_module, "SLOW_LOCK_SECONDS", 0.05)

    def slow_holder() -> None:
        with exclusive_lock(lock_path):
            time.sleep(0.1)

    with caplog.at_level(logging.INFO, logger="project_board.client.locks"):
        holder = threading.Thread(target=slow_holder)
        holder.start()
        time.sleep(0.02)
        with exclusive_lock(lock_path):
            pass
        holder.join(2)

    messages = [record.getMessage() for record in caplog.records]
    holds = [message for message in messages if "slow lock hold" in message]
    waits = [message for message in messages if "slow lock wait" in message]
    assert len(holds) == 1 and "slow_holder" in holds[0], messages
    assert len(waits) == 1 and "test_a_slow_lock_wait_and_hold_are_logged_once_with_their_holder" in waits[0], messages
