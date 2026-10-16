"""W448: pending_worker_mail_refs opens only inbox files it has not read before.

The live split lines on dev-main (2026-10-16, AE b4f7d7cf) showed every slow
call was one worker with 163 set-aside messages: scan 1-2.3 s of wall time,
0.04-0.06 s of CPU. Each of the 163 files was re-opened on every wake decision,
and each read waited for the busy relay's interpreter lock. An inbox file is
named by its message id and that message's ref never changes, so a mailbox whose
files are unchanged is now listed, not re-read. The refs it returns are the same.
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from project_board.client import store as store_module
from project_board.client.io import FileLockBusy
from project_board.client.store import SharedFieldStore

PROJECT = "project-one"
PROJECT_REF = f"work:project:{PROJECT}"
WORKER = "codex-11111111-1111-4111-8111-111111111111"
SENDER = "codex-22222222-2222-4222-8222-222222222222"


@pytest.fixture
def field(tmp_path: Path, monkeypatch) -> SharedFieldStore:
    monkeypatch.setattr(store_module, "_INBOX_REFS", {}, raising=False)
    store = SharedFieldStore(tmp_path / "shared-field")
    store.initialize(field_id="w448-cache")
    for name in (WORKER, SENDER):
        session = name.split("-", 1)[1]
        store.register_worker(
            worker_name=name, runtime_kind="codex", runtime_session_id=session,
            capabilities=[], authority_label=f"authority:{session}",
        )
        store.listen_worker(name)
    store.create_project(project_id=PROJECT, title="Mail", goal="Read once.", owner="operator")
    store.sync_worker_attendances(WORKER, [PROJECT_REF])
    store.sync_worker_attendances(SENDER, [PROJECT_REF])
    for number in range(3):
        _send(store, number)
    return store


def _send(field: SharedFieldStore, number: int) -> None:
    field.send_mail(PROJECT, sender=SENDER, recipient=WORKER, kind="request",
                    subject=f"Task {number}", body="Do it.", idempotency_key=f"w448-cache-{number}")


def _counting_reads(monkeypatch) -> list[Path]:
    opened: list[Path] = []
    real = store_module.read_json

    def read_json(path, *args, **kwargs):
        if Path(path).parent.name == "inbox":
            opened.append(Path(path))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(store_module, "read_json", read_json)
    return opened


def _full_listing(field: SharedFieldStore) -> list[str]:
    """What the pre-W448 scan returned: every inbox file read, in name order."""

    refs = []
    for project_id in ("", PROJECT):
        inbox = field._mail_root(project_id, WORKER) / "inbox"
        for path in sorted(inbox.glob("*.json")) if inbox.exists() else []:
            ref = str(store_module.read_json(path).get("message_ref") or "")
            if ref:
                refs.append(ref)
    return refs


def test_an_unchanged_inbox_is_listed_not_reread(field, monkeypatch) -> None:
    expected = _full_listing(field)
    opened = _counting_reads(monkeypatch)

    assert field.pending_worker_mail_refs(WORKER, wait=False) == expected
    assert len(opened) == 3

    opened.clear()
    assert field.pending_worker_mail_refs(WORKER, wait=False) == expected
    assert opened == [], "an unchanged mailbox is not re-read"

    _send(field, 3)
    opened.clear()
    refs = field.pending_worker_mail_refs(WORKER, wait=False)
    assert len(opened) == 1, "only the new message's file is opened"
    assert refs == _full_listing(field) and len(refs) == 4


def test_mail_that_leaves_the_inbox_drops_out(field) -> None:
    assert len(field.pending_worker_mail_refs(WORKER, wait=False)) == 3
    [leased] = field.pull_mail(PROJECT, worker_name=WORKER, lease_owner="test", limit=1)

    refs = field.pending_worker_mail_refs(WORKER, wait=False)

    assert leased["message_ref"] not in refs
    assert refs == _full_listing(field) and len(refs) == 2


def test_a_slow_lock_free_call_logs_its_lock_free_phase(field, caplog, monkeypatch) -> None:
    """Review of #830: the lock-free fallback (mailbox lock busy) is timed too."""

    def busy(path, *, wait=True):
        raise FileLockBusy(str(path))

    clock = iter(x * 0.25 for x in range(1000))
    monkeypatch.setattr(store_module, "exclusive_lock", busy)
    monkeypatch.setattr(store_module, "_log_busy_mail_lock", lambda path: None)
    monkeypatch.setattr(store_module, "time", SimpleNamespace(monotonic=lambda: next(clock),
                                                              thread_time=lambda: 0.0))
    caplog.set_level(logging.INFO, logger="problem_board.local.store")

    refs = field.pending_worker_mail_refs(WORKER, wait=False)

    assert len(refs) == 3
    [record] = [r for r in caplog.records if "slow pending mail refs" in r.getMessage()]
    line = record.getMessage()
    assert "lock_free=0.000" not in line and "files=0" in line and "opened=0" in line
