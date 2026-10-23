"""W943 F2-D: the project syncs compare before the lock and write only on change.

On 2026-10-23, after the per-owner reconciliation and the retention locks,
the project lock was the only lock with slow waits left on dev-main: 113 an
hour, max 7.3 s. Its waiters were seven syncs that every channel runs on every
heartbeat, each taking the project lock (and reading the project) before it
decided whether anything had changed.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from project_board.client.io import exclusive_lock
from project_board.client.store import SharedFieldStore

PROJECT = "project-one"
WORKER = "codex-api"
TEAM = [{"worker_name": WORKER, "worker_alias": "api", "role": "worker", "presence": "online"}]
COORDINATOR = {"state": "held", "holder": {"worker_name": "codex-main"}, "revision": 3}
ROLES = {"keeper": {"state": "held", "holder": {"worker_name": "pb-knowledge"}, "revision": 2}}
REPOSITORIES = [{"alias": "app", "url": "https://github.com/example/app.git", "role": "work"}]
FILES = [{"purpose": "facts", "alias": "app", "path": "docs/facts.md"}]


@pytest.fixture
def field(tmp_path: Path) -> SharedFieldStore:
    store = SharedFieldStore(tmp_path / "field")
    store.initialize(field_id="test-field")
    store.register_worker(worker_name=WORKER, runtime_kind="codex", capabilities=[], authority_label="authority:api")
    store.create_project(project_id=PROJECT, title="Syncs", goal="Write only on change.", owner="operator")
    return store


SYNCS = {
    "team": lambda f: f.sync_project_team(PROJECT, TEAM),
    "coordinator": lambda f: f.sync_project_coordinator(PROJECT, COORDINATOR),
    "roles": lambda f: f.sync_project_roles(PROJECT, ROLES),
    "repositories": lambda f: f.sync_project_repositories(PROJECT, REPOSITORIES, revision=4, commit_identity_email="a@example.com"),
    "card": lambda f: f.sync_project_card(PROJECT, goal="Goal", facts=[{"label": "L", "value": "V"}], revision=5),
    "files": lambda f: f.sync_project_files(PROJECT, files=FILES, revision=6, edit_allowed=True, worker_name=WORKER),
    "assignments": lambda f: f.sync_active_assignments(PROJECT, worker_name=WORKER, assignment_refs=[]),
}
FILES_OF = {"team": "team.json", "coordinator": "coordinator.json", "roles": "roles.json",
            "repositories": "repositories.json", "card": "card.json", "files": "files.json"}


def _returns_while_the_project_lock_is_held(field, call, seconds=0.5):
    held, release = threading.Event(), threading.Event()

    def hold():
        with exclusive_lock(field._project_lock(PROJECT)):
            held.set()
            release.wait(seconds * 4)

    holder = threading.Thread(target=hold)
    holder.start()
    held.wait()
    try:
        started = time.monotonic()
        call(field)
        return time.monotonic() - started < seconds
    finally:
        release.set()
        holder.join()


@pytest.mark.parametrize("name", sorted(SYNCS))
def test_an_unchanged_reply_takes_no_project_lock_and_writes_nothing(field, name):
    SYNCS[name](field)
    path = field._project_dir(PROJECT) / FILES_OF[name] if name in FILES_OF else None
    before = path.stat().st_mtime_ns if path else None

    assert _returns_while_the_project_lock_is_held(field, SYNCS[name])
    if path:
        assert path.stat().st_mtime_ns == before


def test_changed_content_is_written(field):
    field.sync_project_team(PROJECT, TEAM)
    field.sync_project_team(PROJECT, [{**TEAM[0], "presence": "away"}])
    assert field.read_project_team(PROJECT)[0]["presence"] == "away"

    field.sync_project_coordinator(PROJECT, COORDINATOR)
    field.sync_project_coordinator(PROJECT, {**COORDINATOR, "acting": True})
    assert field.read_project_coordinator(PROJECT)["acting"] is True

    field.sync_project_roles(PROJECT, ROLES)
    field.sync_project_roles(PROJECT, {"keeper": {**ROLES["keeper"], "available": True}})
    assert field.read_project_roles(PROJECT)["keeper"]["available"] is True

    field.sync_project_card(PROJECT, goal="Goal", facts=[], revision=5)
    assert field.sync_project_card(PROJECT, goal="New goal", facts=[], revision=6) is True
    assert field.read_project_card(PROJECT)["goal"] == "New goal"


def test_an_older_revision_is_never_written(field):
    field.sync_project_coordinator(PROJECT, COORDINATOR)
    field.sync_project_coordinator(PROJECT, {**COORDINATOR, "revision": 2, "acting": True})
    assert field.read_project_coordinator(PROJECT)["acting"] is False

    field.sync_project_roles(PROJECT, ROLES)
    field.sync_project_roles(PROJECT, {"keeper": {**ROLES["keeper"], "revision": 1, "available": True}})
    assert field.read_project_roles(PROJECT)["keeper"]["available"] is False

    assert field.sync_project_repositories(PROJECT, REPOSITORIES, revision=4, commit_identity_email="a@example.com") is True
    assert field.sync_project_repositories(PROJECT, [], revision=3, commit_identity_email="a@example.com") is False
    assert len(field.read_project_repositories(PROJECT)["repositories"]) == 1
    # An equal revision with another email is still written (W368).
    assert field.sync_project_repositories(PROJECT, REPOSITORIES, revision=4, commit_identity_email="b@example.com") is True

    field.sync_project_card(PROJECT, goal="Goal", facts=[], revision=5)
    assert field.sync_project_card(PROJECT, goal="Old", facts=[], revision=4) is False

    field.sync_project_files(PROJECT, files=FILES, revision=6)
    assert field.sync_project_files(PROJECT, files=[], revision=5) is False
    assert len(field.read_project_files(PROJECT)["files"]) == 1


def test_a_changed_permission_alone_is_written_without_the_project_lock(field):
    field.sync_project_files(PROJECT, files=FILES, revision=6, edit_allowed=True, worker_name=WORKER)

    assert _returns_while_the_project_lock_is_held(
        field, lambda f: f.sync_project_files(PROJECT, files=FILES, revision=6, edit_allowed=False, worker_name=WORKER))
    assert field.read_project_files(PROJECT, worker_name=WORKER)["edit_allowed"] is False


def test_assignments_read_only_this_workers_rows_and_lock_only_to_remove_a_stale_one(field, monkeypatch):
    store = field._assignments(PROJECT)
    stale = {"assignment_ref": "work:assignment:20261023T000000Z:assignment_aaaa:stale", "worker_name": WORKER,
             "state": "assigned"}
    current = {**stale, "assignment_ref": "work:assignment:20261023T000000Z:assignment_bbbb:current"}
    listed, removed = [], []
    monkeypatch.setattr(type(store), "list_pending",
                        lambda self, *, worker_name="": listed.append(worker_name) or [dict(stale), dict(current)])
    monkeypatch.setattr(type(store), "remove", lambda self, object_id, *, worker_name: removed.append(object_id))

    # Every row is still active: no lock, nothing removed.
    assert _returns_while_the_project_lock_is_held(
        field, lambda f: f.sync_active_assignments(PROJECT, worker_name=WORKER,
                                                   assignment_refs=[stale["assignment_ref"], current["assignment_ref"]]))
    assert removed == []
    # One is gone from the heartbeat: it is removed, under the lock.
    assert field.sync_active_assignments(PROJECT, worker_name=WORKER, assignment_refs=[current["assignment_ref"]]) == 1
    assert len(removed) == 1
    assert set(listed) == {WORKER}
