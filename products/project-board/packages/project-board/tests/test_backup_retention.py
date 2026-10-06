"""W423: runtime-window backups live in one managed folder and only the newest stays.

2026-09-30: every runtime window left a table dump in the scratch folder of
whichever agent ran it; one host held 24 in one agent's folder and 9 in
another's, and its disk filled. The operator's rule: keep the newest one. A
plain SQL gzip dump is checked as what it is (gzip stream, pg_dump markers),
never with ``pg_restore --list``, and no check is called restore proof.
"""

from __future__ import annotations

import gzip
import json
import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from project_board.client import backups, cli
from project_board.contract.errors import DomainError
from procedure_reference import reference_text

PROJECT_REF = "work:project:demo-project"


def plain_dump(path: Path, *, tables: int = 2, complete: bool = True) -> Path:
    lines = ["--", "-- PostgreSQL database dump", "--", ""]
    for index in range(tables):
        lines += [f"CREATE TABLE demo.problem_board_t{index} (", "    id text", ");", ""]
    if complete:
        lines += ["--", "-- PostgreSQL database dump complete", "--"]
    with gzip.open(path, "wb") as stream:
        stream.write(("\n".join(lines) + "\n").encode())
    return path


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    return backups.backup_directory(tmp_path / "backups", "demo-project", create=True)


def test_the_folder_is_the_operators_choice_private_and_never_in_a_git_tree(tmp_path):
    with pytest.raises(DomainError) as unset:
        backups.backup_directory("", "demo-project", create=True)
    assert unset.value.code == "backup_root_unset" and "pb host configure --backup-root" in str(unset.value)
    clone = tmp_path / "clone"
    subprocess.run(["git", "init", "-q", str(clone)], check=True)
    with pytest.raises(DomainError) as in_tree:
        backups.backup_directory(clone / "backups", "demo-project", create=True)
    assert in_tree.value.code == "backup_root_in_git_tree"
    assert not (clone / "backups").exists()
    # Listing never creates it; naming a file does, private to the host user.
    listed = backups.backup_directory(tmp_path / "root", "demo-project")
    assert not listed.exists()
    created = backups.backup_directory(tmp_path / "root", "demo-project", create=True)
    assert stat.S_IMODE(created.stat().st_mode) == 0o700


def test_a_plain_sql_gzip_dump_is_checked_as_gzip_and_pg_dump_never_with_pg_restore(folder, monkeypatch):
    def no_pg_restore(*_args, **_kwargs):
        raise AssertionError("pg_restore --list cannot read a plain SQL dump")

    monkeypatch.setattr(backups.subprocess, "run", no_pg_restore)
    good = backups.verify_backup(plain_dump(folder / "good.sql.gz"), "plain-sql-gzip")
    assert good["ok"] and good["tables"] == 2
    assert good["restore_proof"] is False and "not restore proof" in good["note"]
    assert "pg_restore" not in good["method"]
    # A dump cut short: no completion marker.
    cut = backups.verify_backup(plain_dump(folder / "cut.sql.gz", complete=False), "plain-sql-gzip")
    assert not cut["ok"] and "completion marker" in cut["detail"]
    # A gzip stream cut short fails its CRC/length check.
    raw = (folder / "good.sql.gz").read_bytes()
    (folder / "torn.sql.gz").write_bytes(raw[: len(raw) // 2])
    torn = backups.verify_backup(folder / "torn.sql.gz", "plain-sql-gzip")
    assert not torn["ok"] and "gzip stream unreadable" in torn["detail"]


def test_a_custom_format_dump_is_checked_with_pg_restore_list(folder, tmp_path):
    archive = folder / "pb-backup-20260930T200000Z.dump"
    archive.write_bytes(b"PGDMP" + b"\x00" * 64)
    listing = tmp_path / "pg_restore"
    listing.write_text(
        "#!/bin/sh\n"
        "[ \"$1\" = --list ] || exit 3\n"
        "cat >/dev/null\n"
        "echo '215; 1259 16390 TABLE demo problem_board_items owner'\n"
        "echo '3300; 0 16390 TABLE DATA demo problem_board_items owner'\n",
        encoding="utf-8",
    )
    listing.chmod(0o755)
    good = backups.verify_backup(archive, "pg-custom", pg_restore=[str(listing)])
    assert good["ok"] and good["tables"] == 1 and good["restore_proof"] is False
    failing = tmp_path / "failing"
    failing.write_text("#!/bin/sh\ncat >/dev/null\necho 'input file appears to be corrupt' >&2\nexit 1\n", encoding="utf-8")
    failing.chmod(0o755)
    bad = backups.verify_backup(archive, "pg-custom", pg_restore=[str(failing)])
    assert not bad["ok"] and "corrupt" in bad["detail"]
    (folder / "plain.dump").write_bytes(b"-- PostgreSQL database dump")
    assert not backups.verify_backup(folder / "plain.dump", "pg-custom", pg_restore=[str(listing)])["ok"]


def test_recording_verifies_the_file_in_the_folder_and_writes_the_manifest(folder, tmp_path):
    path = plain_dump(backups.new_backup_path(folder, "plain-sql-gzip"))
    entry = backups.record_backup(folder, path, "plain-sql-gzip", label="board reload at abc123", recorded_by="coordinator-one")
    assert entry["verification"]["ok"] and entry["sha256"] and entry["recorded_by"] == "coordinator-one"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    manifest = backups.read_manifest(folder)
    assert [item["file"] for item in manifest["backups"]] == [path.name]
    # Only a file written into the folder, with its format's suffix.
    outside = plain_dump(tmp_path / "pb-backup-outside.sql.gz")
    with pytest.raises(DomainError) as elsewhere:
        backups.record_backup(folder, outside, "plain-sql-gzip", label="", recorded_by="coordinator-one")
    assert elsewhere.value.code == "backup_outside_directory"
    with pytest.raises(DomainError) as mismatch:
        backups.record_backup(folder, path, "pg-custom", label="", recorded_by="coordinator-one")
    assert mismatch.value.code == "backup_format_mismatch"


def _three_backups(folder: Path) -> list[Path]:
    paths = []
    for index, stamp in enumerate(("20260926T163018Z", "20260928T101500Z", "20260930T194500Z")):
        path = plain_dump(folder / f"pb-backup-{stamp}.sql.gz", tables=index + 1)
        os.utime(path, (1_790_000_000 + index * 100_000,) * 2)
        backups.record_backup(folder, path, "plain-sql-gzip", label=f"window {index}", recorded_by="coordinator-one")
        paths.append(path)
    return paths


def test_after_the_all_clear_only_the_newest_verified_backup_stays(folder):
    old, middle, newest = _three_backups(folder)
    stray = folder / "someone-elses.sql.gz"
    stray.write_bytes(b"not listed")
    with pytest.raises(DomainError) as no_all_clear:
        backups.prune_backups(folder, all_clear="", pruned_by="coordinator-one", apply=True)
    assert no_all_clear.value.code == "backup_prune_needs_all_clear"
    report = backups.prune_backups(folder, all_clear="all-clear message ref", pruned_by="coordinator-one")
    assert report["state"] == "report_only" and report["kept"] == newest.name
    assert sorted(report["would_delete"]) == sorted([old.name, middle.name])
    assert old.exists() and middle.exists()
    pruned = backups.prune_backups(folder, all_clear="all-clear message ref", pruned_by="coordinator-one", apply=True)
    assert pruned["state"] == "pruned" and pruned["kept"] == newest.name
    assert not old.exists() and not middle.exists() and newest.exists()
    # A file the manifest does not list is never touched, and the listing names it.
    assert stray.exists()
    listing = backups.backup_report(folder)
    assert [item["file"] for item in listing["backups"]] == [newest.name]
    assert listing["unmanaged"] == [stray.name]
    manifest = backups.read_manifest(folder)
    assert manifest["prunes"][-1]["all_clear"] == "all-clear message ref"
    assert manifest["prunes"][-1]["kept"] == newest.name


def test_pruning_refuses_when_the_newest_backup_is_not_a_verified_copy(folder):
    old, _middle, newest = _three_backups(folder)
    torn = folder / "pb-backup-20260930T235900Z.sql.gz"
    torn.write_bytes(newest.read_bytes()[:40])
    os.utime(torn, (1_791_000_000,) * 2)
    backups.record_backup(folder, torn, "plain-sql-gzip", label="window 3", recorded_by="coordinator-one")
    with pytest.raises(DomainError) as unverified:
        backups.prune_backups(folder, all_clear="all clear", pruned_by="coordinator-one", apply=True)
    assert unverified.value.code == "backup_prune_newest_unverified"
    assert old.exists()
    # A newest backup changed after it was recorded is not the verified copy either.
    torn.unlink()
    manifest_path = folder / backups.MANIFEST_NAME
    value = json.loads(manifest_path.read_text())
    value["backups"] = [item for item in value["backups"] if item["file"] != torn.name]
    manifest_path.write_text(json.dumps(value))
    newest.write_bytes(newest.read_bytes() + b"x")
    with pytest.raises(DomainError) as changed:
        backups.prune_backups(folder, all_clear="all clear", pruned_by="coordinator-one", apply=True)
    assert "SHA-256" in str(changed.value) and old.exists()


def _args(tmp_path: Path, **values):
    base = {"project_ref": PROJECT_REF, "config": str(tmp_path / "relay.json"), "new": False, "record": "",
            "backup_format": "plain-sql-gzip", "label": "", "pg_restore": "pg_restore", "prune": False,
            "all_clear": "", "apply": False}
    return SimpleNamespace(**{**base, **values})


class Field:
    def __init__(self, holder: str, team=()):
        self.holder = holder
        self.team = list(team)

    def read_project_coordinator(self, _project_id):
        return {"holder": {"worker_name": self.holder}}

    def read_project_team(self, _project_id):
        return self.team


def test_the_manifest_is_the_coordinators_and_listing_is_open(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.HostRelayConfig, "load", classmethod(lambda cls, _path: SimpleNamespace(backup_root=str(tmp_path / "backups"))))
    worker = SimpleNamespace(worker_name="worker-two")
    coordinator = SimpleNamespace(worker_name="coordinator-one")
    with pytest.raises(DomainError) as refused:
        cli._worker_backup(Field("coordinator-one"), worker, _args(tmp_path, new=True))
    assert refused.value.code == "backup_manifest_coordinator_owned"
    assert not (tmp_path / "backups").exists()
    named = cli._worker_backup(Field("coordinator-one"), coordinator, _args(tmp_path, new=True))
    assert named["write_to"].startswith(str(tmp_path / "backups" / "demo-project" / "pb-backup-"))
    plain_dump(Path(named["write_to"]))
    recorded = cli._worker_backup(Field("coordinator-one"), coordinator, _args(tmp_path, record=named["write_to"], label="reload"))
    assert recorded["recorded"]["verification"]["ok"]
    # A team member labelled coordinator may act too; anyone may list.
    team = [{"worker_name": "worker-two", "role": "coordinator"}]
    assert cli._worker_backup(Field("coordinator-one", team), worker, _args(tmp_path, prune=True, all_clear="ref"))["state"] == "report_only"
    listing = cli._worker_backup(Field("coordinator-one"), worker, _args(tmp_path))
    assert [item["file"] for item in listing["backups"]] == [Path(named["write_to"]).name]


def test_the_host_config_carries_the_backup_root_outside_any_git_tree(tmp_path):
    from relay_helpers import make_host

    from project_board.client.host_config import HostRelayConfig, update_host_config

    host, _identity, _channel = make_host(tmp_path)
    assert host.backup_root == ""
    updated = update_host_config(host.path, backup_root=str(tmp_path / "pb-backups"))
    assert updated.backup_root == str((tmp_path / "pb-backups").resolve())
    assert HostRelayConfig.load(host.path).backup_root == updated.backup_root
    assert cli._host_view(updated)["backups"] == {"root": updated.backup_root}
    clone = tmp_path / "clone"
    subprocess.run(["git", "init", "-q", str(clone)], check=True)
    with pytest.raises(DomainError) as in_tree:
        update_host_config(host.path, backup_root=str(clone / "dumps"))
    assert in_tree.value.code == "backup_root_in_git_tree"
    assert update_host_config(host.path, backup_root="").backup_root == ""
    args = cli.build_parser().parse_args(["host", "configure", "--backup-root", "/srv/pb-backups"])
    assert args.backup_root == "/srv/pb-backups"


def test_the_runtime_actions_procedure_owns_the_backup_step():
    """W423 acceptance 4 and 5: the step in runtime-actions, the lifetime rule in
    project-workspace, a pointer from the coordinator, the commands in the docs."""

    procedures = Path(cli.__file__).resolve().parents[1] / "procedures" / "problem-board-worker" / "references"

    def read(path: Path) -> str:
        return " ".join(path.read_text(encoding="utf-8").split())

    actions = read(procedures / "runtime-actions.md")
    assert "## Runtime-Window Database Backups" in actions
    for command in (
        "pb worker backup --project-ref <project> --new --dump-format plain-sql-gzip",
        "pb worker backup --project-ref <project> --record <file> --dump-format plain-sql-gzip",
        "pb worker backup --project-ref <project> --prune --all-clear",
        "pb host configure --backup-root",
    ):
        assert command in actions, command
    assert "`pg_restore --list` cannot read a plain dump" in actions
    assert "Either check proves integrity, never that the backup restores" in actions
    assert "never an agent's scratch folder" in actions
    coordinator = " ".join(reference_text(procedures / "coordinator.md").split())
    assert "back up the board tables into the host's backup folder" in coordinator
    assert "(../runtime-actions.md#runtime-window-database-backups)" in coordinator
    workspace = read(procedures / "project-workspace.md")
    assert "Runtime-window database backups follow the same lifetime rule" in workspace
    assert "(runtime-actions.md#runtime-window-database-backups)" in workspace
    docs = Path(cli.__file__).resolve().parents[5] / "docs" / "storage-and-retention.md"
    storage = read(docs)
    assert "## Runtime-Window Database Backups" in storage and "`restore_proof: false`" in storage
    # No procedure tells anyone to put a dump in a scratch folder any more.
    for page in procedures.glob("*.md"):
        assert "<scratch>/pb-backup" not in page.read_text(encoding="utf-8"), page.name


def test_the_dump_format_flag_survives_the_output_format_flag():
    """Every pb command reads --format as its output form, before parsing."""

    from project_board.client.render import select_format

    argv, chosen = select_format(
        ["worker", "backup", "--project-ref", PROJECT_REF, "--new", "--dump-format", "pg-custom", "--format", "brief"], {}
    )
    args = cli.build_parser().parse_args(argv)
    assert chosen == "brief" and args.backup_format == "pg-custom" and args.new
