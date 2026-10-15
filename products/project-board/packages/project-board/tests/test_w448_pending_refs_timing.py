"""W448: a slow pending_worker_mail_refs call says where its time went.

On dev-main (2026-10-15) the relay's store thread held pending_worker_mail_refs
for 1-2 s six to eight times every 15 minutes, while reading every inbox file of
the largest mailbox (163 files, 1.2 MB) took 6.6 ms. A call of 1 s or more now
logs its wall time per phase and the thread's CPU time, so one live window shows
whether the thread waited or worked. Nothing else changes.
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from project_board.client import store as store_module
from project_board.client.store import SharedFieldStore

PROJECT = "project-one"
PROJECT_REF = f"work:project:{PROJECT}"
WORKER = "codex-11111111-1111-4111-8111-111111111111"
SENDER = "codex-22222222-2222-4222-8222-222222222222"


@pytest.fixture
def field(tmp_path: Path) -> SharedFieldStore:
    store = SharedFieldStore(tmp_path / "shared-field")
    store.initialize(field_id="w448-timing")
    for name in (WORKER, SENDER):
        session = name.split("-", 1)[1]
        store.register_worker(
            worker_name=name, runtime_kind="codex", runtime_session_id=session,
            capabilities=[], authority_label=f"authority:{session}",
        )
        store.listen_worker(name)
    store.create_project(project_id=PROJECT, title="Mail", goal="Time it.", owner="operator")
    store.sync_worker_attendances(WORKER, [PROJECT_REF])
    store.sync_worker_attendances(SENDER, [PROJECT_REF])
    for number in range(3):
        store.send_mail(PROJECT, sender=SENDER, recipient=WORKER, kind="request",
                        subject=f"Task {number}", body="Do it.", idempotency_key=f"w448-timing-{number}")
    return store


def _records(caplog):
    return [r for r in caplog.records if "slow pending mail refs" in r.getMessage()]


def test_a_fast_call_logs_nothing(field, caplog) -> None:
    caplog.set_level(logging.INFO, logger="problem_board.local.store")
    assert len(field.pending_worker_mail_refs(WORKER, wait=False)) == 3
    assert _records(caplog) == []


def test_a_slow_call_logs_its_phases_and_cpu_once(field, caplog, monkeypatch) -> None:
    expected = field.pending_worker_mail_refs(WORKER, wait=False)
    clock = iter(x * 0.25 for x in range(1000))
    cpu = iter((0.0, 0.01))
    monkeypatch.setattr(store_module, "time", SimpleNamespace(monotonic=lambda: next(clock),
                                                              thread_time=lambda: next(cpu)), raising=False)
    caplog.set_level(logging.INFO, logger="problem_board.local.store")

    assert field.pending_worker_mail_refs(WORKER, wait=False) == expected

    [record] = _records(caplog)
    line = record.getMessage()
    assert f"worker={WORKER}" in line and "wait=False" in line and "cpu_seconds=0.010" in line
    for phase in ("worker_read=", "lock=", "lock_free=", "recover=", "scan="):
        assert phase in line
    assert "files=3" in line and "refs=3" in line
