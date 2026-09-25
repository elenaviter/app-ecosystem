"""The host keeps who acts as coordinator now, never going back a revision (W313)."""

from __future__ import annotations

from project_board.client.store import SharedFieldStore

PROJECT = "quickstart-works"


def _view(holder: str, revision: int) -> dict:
    return {
        "state": "held",
        "holder": {"worker_name": holder.upper(), "worker_alias": holder, "attending": True},
        "home": {"worker_name": "claude-main", "worker_alias": "claude-main", "attending": True},
        "acting": holder != "claude-main",
        "revision": revision,
        "home_available": True,
        "unexpected": "dropped",
    }


def test_the_holder_is_kept_bounded_and_an_older_revision_never_replaces_a_newer_one(tmp_path):
    store = SharedFieldStore(tmp_path / "field")
    store.initialize(field_id="holder")
    store.create_project(project_id=PROJECT, title="Quickstart works", goal="Onboard agents.", owner="operator")
    assert store.read_project_coordinator(PROJECT) == {}

    store.sync_project_coordinator(PROJECT, _view("codex-main", 3))
    kept = store.read_project_coordinator(PROJECT)
    assert kept["holder"] == {"worker_name": "codex-main", "worker_alias": "codex-main", "attending": True}
    assert kept["revision"] == 3 and kept["acting"] is True
    assert "unexpected" not in kept

    # A heartbeat answered before the last hand-over arrives late.
    assert store.sync_project_coordinator(PROJECT, _view("claude-main", 2)) is None
    assert store.read_project_coordinator(PROJECT)["holder"]["worker_alias"] == "codex-main"

    store.sync_project_coordinator(PROJECT, _view("claude-main", 4))
    assert store.read_project_coordinator(PROJECT)["holder"]["worker_alias"] == "claude-main"
