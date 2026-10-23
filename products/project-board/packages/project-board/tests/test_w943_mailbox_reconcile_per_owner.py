"""W943 F2-A: mailbox reconciliation runs once per owner view per change.

On 2026-10-23 dev-main's relay ran the recipients sync and the whole-project
mailbox reconciliation on every heartbeat of every channel, about twenty
times a cycle, queueing on the project lock (reads up to 24.7 s). The board
answers each heartbeat with the recipients its owner may see, so the one
shared mail-recipients.json flipped between owners' views and the
reconciliation decided with whichever was written last.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from project_board.client import relay
from project_board.client import store as store_module
from project_board.client.host_config import mailbox_reconcile_interval
from project_board.client.io import exclusive_lock
from project_board.client.store import SharedFieldStore, mail_recipient_view

PROJECT = "project-one"
PROJECT_REF = f"work:project:{PROJECT}"
A1, A2, B1 = "claude-a1", "claude-a2", "claude-b1"
GONE = "claude-gone"
OWNERS = {A1: "user-a", A2: "user-a", B1: "user-b", GONE: "user-a"}


@pytest.fixture
def field(tmp_path: Path) -> SharedFieldStore:
    store = SharedFieldStore(tmp_path / "shared-field")
    store.initialize(field_id="test-field")
    for name in (A1, A2, B1, GONE):
        store.register_worker(worker_name=name, runtime_kind="claude-code", capabilities=[], authority_label=f"authority:{name}")
        store.record_worker_board_record(name, {"owner": {"user_id": OWNERS[name], "source": "card"}})
    store.create_project(project_id=PROJECT, title="Mail", goal="Reconcile once.", owner="operator")
    for name in (A1, A2, B1, GONE):
        store.sync_worker_attendances(name, [PROJECT_REF])
    return store


def _team(*extra):
    return [{"worker_name": name, "worker_alias": name} for name in (A1, A2, B1)] + list(extra)


GONE_RETIRED = {"worker_name": GONE, "worker_alias": "gone", "pool_status": "retired",
                "retired_at": "2026-10-20T10:00:00Z", "retirement_reason": "Replaced."}


def _counting(field, monkeypatch):
    calls = []
    original = field.reconcile_project_mailboxes

    def counted(project_id, *, reporter_worker_name, view=""):
        calls.append((reporter_worker_name, view))
        return original(project_id, reporter_worker_name=reporter_worker_name, view=view)

    monkeypatch.setattr(field, "reconcile_project_mailboxes", counted)
    return calls


def _beat(field, worker, recipients, interval=300):
    return field.reconcile_project_mailboxes_when_due(
        PROJECT, recipients, reporter_worker_name=worker, interval_seconds=interval)


def test_two_owners_alternating_heartbeats_reconcile_once_each(field, monkeypatch):
    calls = _counting(field, monkeypatch)
    a_view, b_view = _team(GONE_RETIRED), _team()

    for _ in range(5):
        _beat(field, A1, a_view)
        _beat(field, B1, b_view)
        _beat(field, A2, a_view)

    assert [reporter for reporter, _ in calls] == [A1, B1]
    assert len({view for _, view in calls}) == 2
    assert _beat(field, A2, a_view)["state"] == "current"


def test_a_recipients_change_runs_on_the_next_heartbeat(field, monkeypatch):
    calls = _counting(field, monkeypatch)
    _beat(field, A1, _team())
    _beat(field, A1, _team())
    _beat(field, A2, _team(GONE_RETIRED))

    assert [reporter for reporter, _ in calls] == [A1, A2]


def test_lagging_views_of_one_owner_do_not_rerun_every_heartbeat(field, monkeypatch):
    calls = _counting(field, monkeypatch)
    old, new = _team(), _team(GONE_RETIRED)

    for _ in range(6):
        _beat(field, A1, new)
        _beat(field, A2, old)

    assert len(calls) == 2


def test_the_interval_reruns_an_unchanged_view(field, monkeypatch):
    calls = _counting(field, monkeypatch)
    _beat(field, A1, _team())
    marker = field._project_dir(PROJECT) / "mail-reconciliation" / f"{mail_recipient_view(field.read_worker(A1), A1)}.json"
    row = json.loads(marker.read_text())
    row["ran_at"] = "2000-01-01T00:00:00Z"
    marker.write_text(json.dumps(row))

    _beat(field, A1, _team())

    assert len(calls) == 2


def test_a_held_view_lock_skips_and_still_recovers_own_receipts(field, monkeypatch):
    calls = _counting(field, monkeypatch)
    recovered = []
    monkeypatch.setattr(store_module, "recover_unpublished_receipts",
                        lambda store, project, *, worker_name: recovered.append(worker_name))
    view = mail_recipient_view(field.read_worker(A1), A1)
    with exclusive_lock(field._project_dir(PROJECT) / "mail-reconciliation" / f"{view}.lock"):
        result = _beat(field, A2, _team())

    assert result == {"state": "current", "view": view, "reason": "reconciling_elsewhere"}
    assert calls == [] and recovered == [A2]


def test_each_owner_view_has_its_own_file_and_the_shared_one_is_not_written(field):
    _beat(field, A1, _team(GONE_RETIRED))
    _beat(field, B1, _team())

    project = field._project_dir(PROJECT)
    assert not (project / "mail-recipients.json").exists()
    files = {path.stem: json.loads(path.read_text()) for path in (project / "mail-recipients").glob("*.json")}
    assert set(files) == {mail_recipient_view(field.read_worker(A1), A1), mail_recipient_view(field.read_worker(B1), B1)}
    merged = field.read_project_mail_recipients(PROJECT)
    assert {row["worker_name"]: row["pool_status"] for row in merged["recipients"]}[GONE] == "retired"


def _mail_for_gone(field):
    field.send_mail(PROJECT, sender=A1, recipient=GONE, kind="request",
                    subject="Task", body="Do it.", idempotency_key="task-gone")
    # The reader's local record is gone, as after its session was removed from
    # this host: only the owners' views know it.
    field._worker_path(GONE).unlink()


@pytest.mark.parametrize("order", [(A1, B1), (B1, A1)])
def test_a_reader_known_only_to_one_owner_is_decided_the_same_whichever_owner_beats_last(field, order):
    _mail_for_gone(field)
    views = {A1: _team(GONE_RETIRED), B1: _team()}
    first, last = order
    # Both owners' directories are written before the mailbox is decided.
    field.sync_project_mail_recipients(PROJECT, views[first], view=mail_recipient_view(field.read_worker(first), first))
    result = _beat(field, last, views[last])

    [archived] = [row for row in result["archived_mailboxes"] if row["recipient"] == GONE]
    assert archived["disposition"] == "retired_recipient"


def test_an_owner_run_leaves_another_owners_mailbox(field):
    field.send_mail(PROJECT, sender=A1, recipient=B1, kind="request",
                    subject="Task", body="Do it.", idempotency_key="task-b1")
    result = _beat(field, A1, _team())

    assert result["mailboxes_left_to_other_views"] >= 1
    assert B1 not in {row["recipient"] for row in result["archived_mailboxes"]}


def test_a_rerun_never_notifies_a_sender_twice(field):
    _mail_for_gone(field)
    first = _beat(field, A1, _team(GONE_RETIRED), interval=300)
    # Force the rerun the crash between the work and the marker would cause.
    (field._project_dir(PROJECT) / "mail-reconciliation" / f"{mail_recipient_view(field.read_worker(A1), A1)}.json").unlink()
    second = _beat(field, A1, _team(GONE_RETIRED), interval=300)

    assert first["archived_count"] == 1 and first["failure_notices"] + first["retirement_pending"]["original_count"] >= 1
    assert second["archived_count"] == 0 and second["failure_notices"] == 0


def test_the_relay_calls_the_per_view_reconciliation_with_its_interval(field, monkeypatch):
    seen = {}
    monkeypatch.setattr(field, "reconcile_project_mailboxes_when_due",
                        lambda project, recipients, *, reporter_worker_name, interval_seconds:
                        seen.update(project=project, worker=reporter_worker_name, interval=interval_seconds) or {"state": "current"})
    adapter = object.__new__(relay.ProblemBoardHostRelayAdapter)
    adapter.field = field
    adapter.config = type("C", (), {"project_id": PROJECT, "worker_name": A1, "mailbox_reconcile_interval_seconds": 120})()

    assert adapter._reconcile_project_mailboxes(_team()) == {"state": "current"}
    assert seen == {"project": PROJECT, "worker": A1, "interval": 120}


@pytest.mark.parametrize("declared, expected", [(None, 300), (0, 300), (5, 30), (600, 600), (10**9, 86_400)])
def test_the_interval_is_configured_in_relay_json_and_bounded(declared, expected):
    relay_json = {} if declared is None else {"mailbox_reconcile_interval_seconds": declared}
    assert mailbox_reconcile_interval(relay_json) == expected
