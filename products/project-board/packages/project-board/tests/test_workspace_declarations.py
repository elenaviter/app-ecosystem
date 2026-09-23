"""A worker declares where on this host it edits one repository for one assignment (W278 part B).

Local only, never sent. The relay reads the declared worktree and publishes
the tracked files in flight. One row per assignment and repository, replaced
when declared again, forgotten with --clear.
"""

from __future__ import annotations

from argparse import Namespace

import pytest

from project_board.client import cli
from project_board.client.store import SharedFieldStore
from project_board.contract.errors import DomainError
from relay_helpers import make_host


def _field_with_worker(tmp_path):
    host, identity, _channel = make_host(tmp_path)
    field = SharedFieldStore(host.field_root)
    field.initialize(field_id="workspaces")
    field.register_worker(
        worker_name=identity.worker_name, worker_identity=identity.worker_identity,
        runtime_kind=identity.runtime_kind, runtime_session_id=identity.runtime_session_id,
        capabilities=[], authority_label="connection-hub:test-profile", control_plane_state="published",
    )
    return host, identity, field


def test_a_declaration_is_one_row_per_assignment_and_repository_and_is_replaced_and_cleared(tmp_path):
    _host, identity, field = _field_with_worker(tmp_path)
    (tmp_path / "wt-a").mkdir()
    (tmp_path / "wt-b").mkdir()
    first = field.declare_workspace(identity.worker_name, assignment_ref="work:assignment:one", repository_ref="repo:ae/products", path=str(tmp_path / "wt-a"))
    assert first["path"] == str((tmp_path / "wt-a").resolve()) and first["declared_at"]
    field.declare_workspace(identity.worker_name, assignment_ref="work:assignment:one", repository_ref="repo:apps/playground", path=str(tmp_path / "wt-b"))
    # Declaring the same assignment and repository again replaces the row.
    field.declare_workspace(identity.worker_name, assignment_ref="work:assignment:one", repository_ref="repo:ae/products", path=str(tmp_path / "wt-b"))
    rows = field.workspaces(identity.worker_name)
    assert sorted((r["repository_ref"], r["path"]) for r in rows) == [
        ("repo:ae/products", str((tmp_path / "wt-b").resolve())),
        ("repo:apps/playground", str((tmp_path / "wt-b").resolve())),
    ]
    assert field.clear_workspace(identity.worker_name, assignment_ref="work:assignment:one", repository_ref="repo:ae/products") == 1
    assert [r["repository_ref"] for r in field.workspaces(identity.worker_name)] == ["repo:apps/playground"]
    assert field.clear_workspace(identity.worker_name, assignment_ref="work:assignment:one") == 1
    assert field.workspaces(identity.worker_name) == []
    with pytest.raises(DomainError) as refused:
        field.declare_workspace(identity.worker_name, assignment_ref="work:assignment:one", repository_ref="repo:ae/products", path=str(tmp_path / "missing"))
    assert refused.value.code == "field_workspace_path_missing"
    assert field.workspaces("nobody-here") == []


def test_pb_worker_workspace_declares_lists_and_clears(tmp_path):
    host, identity, field = _field_with_worker(tmp_path)
    (tmp_path / "wt").mkdir()
    base = {
        "command": "worker", "worker_command": "workspace", "config": str(host.path),
        "runtime_kind": identity.runtime_kind, "runtime_session_id": identity.runtime_session_id,
        "project_ref": None, "assignment_ref": "", "repository": "", "path": "", "clear": False, "list": False,
    }
    declared = cli._worker_command(Namespace(**{**base, "assignment_ref": "work:assignment:one", "repository": "repo:ae/products", "path": str(tmp_path / "wt")}))
    assert declared["declared"]["repository_ref"] == "repo:ae/products"
    listed = cli._worker_command(Namespace(**{**base, "list": True}))
    assert [r["assignment_ref"] for r in listed["workspaces"]] == ["work:assignment:one"]
    with pytest.raises(ValueError):
        cli._worker_command(Namespace(**base))
    cleared = cli._worker_command(Namespace(**{**base, "clear": True, "assignment_ref": "work:assignment:one"}))
    assert cleared["cleared"] == 1 and cleared["workspaces"] == []
    assert field.workspaces(identity.worker_name) == []
