"""W554: the From source lines work the same on any host.

Operator, 2026-10-05: "the user should have no any idea if this is new install
or no. it simply must work smoothly and easy. with couple of lines." and "the
client does not care if machine new or existing". On the operator's second
machine, pb recorded the released 2026.9.27.2142 on 2026-09-27; after a pip
install of 2026.10.3.811 from source, `pb procedure install` stopped with
work_client_release_selection_mismatch. Now the package that runs `pb procedure
install` becomes the host's pb: the selection and ~/.local/bin/pb follow it.
A host that selected a source snapshot keeps it (W495).
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from project_board.client import cli, entrypoint, relay_source, source_control
from project_board.client.release_install import install_launcher
from project_board.contract.errors import DomainError

OLD, NEW = "2026.9.27.2142", "2026.10.3.811"


def _executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _host(tmp_path: Path, monkeypatch, selection: dict) -> Path:
    config = tmp_path / "target" / "relay.json"
    monkeypatch.setenv("PROBLEM_BOARD_CONFIG", str(config))
    relay_source.write_selection(relay_source.client_source_root(config), selection)
    monkeypatch.setattr(source_control, "installed_release_source", lambda: {"mode": "released", "version": NEW})
    return config


def test_procedure_install_runs_on_a_host_that_recorded_another_release(tmp_path):
    config = tmp_path / "target" / "relay.json"
    relay_source.write_selection(relay_source.client_source_root(config), relay_source.released_selection(OLD))
    installed = {"mode": "released", "version": NEW}

    for argv in (
        ["procedure", "install", "--target", "claude-code"],
        ["--format", "brief", "procedure", "install", "--target", "claude-code"],
        ["--format=brief", "procedure", "install", "--target", "claude-code"],
        ["procedure", "--format", "brief", "install", "--target", "claude-code"],
        ["procedure", "install", "--target", "claude-code", "--format", "brief"],
    ):
        assert entrypoint._selected_command(argv, current_source=installed, config_path=config) is None, argv
    # Any other command still refuses until the host's pb is settled.
    for argv in (["status"], ["--format", "brief", "procedure", "show"], ["--format", "procedure", "install"]):
        with pytest.raises(DomainError) as refusal:
            entrypoint._selected_command(argv, current_source=installed, config_path=config)
        assert refusal.value.code == "work_client_release_selection_mismatch", argv


class _FakeController:
    """Records the host switch; the real one builds a release and restarts relays."""

    calls: list = []

    def __init__(self, config, launcher_path):
        self.config = config
        self.launcher = launcher_path

    def use_release(self, *, expect_version, wait_seconds):
        _FakeController.calls.append(("use-release", expect_version))
        return {"state": "activated", "version": expect_version}

    def use_code(self, *, repository, ref, expect, wait_seconds):
        _FakeController.calls.append(("use-code", str(repository), ref, expect))
        return {"state": "activated", "commit": ref}


def _snapshot_host(tmp_path, monkeypatch, commit, name="target"):
    """A configured host whose selection is a source snapshot at `commit`, as use-code records it."""

    config = tmp_path / name / "relay.json"
    monkeypatch.setenv("PROBLEM_BOARD_CONFIG", str(config))
    monkeypatch.setattr(relay_source, "read_selection",
                        lambda root: {"schema": relay_source.SELECTION_SCHEMA, "mode": "snapshot", "commit": commit})
    monkeypatch.setattr(source_control, "installed_release_source", lambda: {"mode": "released", "version": NEW})
    return config


def _switching(tmp_path, monkeypatch, *, checkout=None):
    launcher = tmp_path / "home" / ".local" / "bin" / "pb"
    _FakeController.calls = []
    monkeypatch.setattr(source_control, "ClientSourceController", lambda config: _FakeController(config, launcher))
    monkeypatch.setattr(cli, "_installed_source_checkout", lambda: checkout)
    return launcher


def test_an_index_install_switches_a_stale_released_host_with_use_release(tmp_path, monkeypatch):
    config = _host(tmp_path, monkeypatch, relay_source.released_selection(OLD))
    launcher = _switching(tmp_path, monkeypatch)
    home = tmp_path / "home"
    running = _executable(tmp_path / "bootstrap" / "bin" / "pb")
    monkeypatch.setattr(sys, "argv", [str(running), "procedure", "install"])
    monkeypatch.setenv("PATH", str(home / ".local" / "bin"))

    result = cli._procedure_command(  # noqa: SLF001 - the command under test
        SimpleNamespace(procedure_command="install", target=["codex"], home=str(home), force=False,
                        allow_downgrade=False, config=None)
    )

    assert _FakeController.calls == [("use-release", NEW)]
    assert result["switched"]["source"] == "release" and result["switched"]["version"] == NEW
    assert result["switched"]["previous"]["mode"] == "released"
    # The skill and the launcher name the host's launcher, not the bootstrap.
    assert result["launcher"] == {"path": str(launcher), "state": "selected"}
    recorded = list((home / ".codex").rglob("installed-by.json"))
    assert recorded and str(launcher) in recorded[0].read_text(encoding="utf-8")


def test_a_checkout_install_switches_a_snapshot_host_with_use_code_at_head(tmp_path, monkeypatch):
    """The reported case: a host pinned to an older snapshot, a from-source install of a newer commit."""

    config = _snapshot_host(tmp_path, monkeypatch, "a" * 40)
    head = "b" * 40
    _switching(tmp_path, monkeypatch, checkout={
        "folder": "/src/app-ecosystem/products/project-board/packages/project-board",
        "repository": "/src/app-ecosystem", "commit": head, "changed": [], "reason": "",
    })

    switched = cli._switch_host_to_installing_package()  # noqa: SLF001

    assert _FakeController.calls == [("use-code", "/src/app-ecosystem", head, head)]
    assert switched["source"] == "code" and switched["commit"] == head
    assert switched["previous"] == {"mode": "snapshot", "commit": "a" * 40}
    # The entrypoint no longer hands this command to the old snapshot.
    for argv in (["procedure", "install", "--target", "claude-code"],
                 ["--format", "brief", "procedure", "install", "--target", "claude-code"]):
        assert entrypoint._selected_command(  # noqa: SLF001
            argv, current_source={"mode": "released", "version": NEW}, config_path=config) is None


def test_a_host_already_on_this_package_changes_nothing(tmp_path, monkeypatch):
    _host(tmp_path, monkeypatch, relay_source.released_selection(NEW))
    _switching(tmp_path, monkeypatch)
    assert cli._switch_host_to_installing_package() is None  # noqa: SLF001
    assert _FakeController.calls == []

    _snapshot_host(tmp_path, monkeypatch, "c" * 40, name="snap")
    _switching(tmp_path, monkeypatch, checkout={
        "folder": "/src/ae", "repository": "/src/ae", "commit": "c" * 40, "changed": [], "reason": ""})
    assert cli._switch_host_to_installing_package() is None  # noqa: SLF001
    assert _FakeController.calls == []


def test_a_new_host_and_a_pb_inside_a_selected_release_change_nothing(tmp_path, monkeypatch):
    _switching(tmp_path, monkeypatch)
    monkeypatch.setenv("PROBLEM_BOARD_CONFIG", "")
    monkeypatch.setenv("HOME", str(tmp_path / "fresh"))
    assert cli._switch_host_to_installing_package() is None  # noqa: SLF001 - no configuration yet

    # The maintainer path: `pb source use-code`, then ~/.local/bin/pb procedure install
    # runs inside the selected release and keeps that snapshot.
    _host(tmp_path, monkeypatch, relay_source.released_selection(OLD))
    monkeypatch.setattr(source_control, "installed_release_source",
                        lambda: {"mode": "snapshot", "release_id": "r1", "commit": "d" * 40})
    assert cli._switch_host_to_installing_package() is None  # noqa: SLF001
    assert _FakeController.calls == []


def test_an_install_that_is_not_a_commit_is_refused_by_name(tmp_path, monkeypatch):
    _host(tmp_path, monkeypatch, relay_source.released_selection(OLD))
    _switching(tmp_path, monkeypatch, checkout={
        "folder": "/src/ae/products/project-board/packages/project-board", "repository": "/src/ae",
        "commit": "e" * 40, "changed": ["products/project-board/packages/project-board/src/x.py"],
        "reason": "uncommitted_changes"})
    with pytest.raises(DomainError) as refused:
        cli._switch_host_to_installing_package()  # noqa: SLF001
    assert refused.value.code == "work_client_install_not_a_commit"
    assert refused.value.details["changed"] == ["products/project-board/packages/project-board/src/x.py"]
    assert _FakeController.calls == []


def test_an_older_installed_release_is_switched_to_too(tmp_path, monkeypatch):
    """Review P3: the person chose the installed package; only an older procedure is refused."""

    _host(tmp_path, monkeypatch, relay_source.released_selection("2026.11.1.100"))
    _switching(tmp_path, monkeypatch)

    switched = cli._switch_host_to_installing_package()  # noqa: SLF001

    assert switched and switched["version"] == NEW
    assert _FakeController.calls == [("use-release", NEW)]


def test_the_installed_checkout_is_read_from_pips_direct_url(tmp_path, monkeypatch):
    import json
    import subprocess

    repo = tmp_path / "ae"
    package = repo / "products" / "project-board" / "packages" / "project-board"
    package.mkdir(parents=True)
    (package / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    run = lambda *argv: subprocess.run(["git", "-C", str(repo), *argv], check=True, capture_output=True)
    run("init", "-q")
    run("-c", "user.email=t@t", "-c", "user.name=t", "add", ".")
    run("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "c")
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()

    class _Dist:
        def __init__(self, url):
            self.url = url

        def read_text(self, name):
            return json.dumps({"url": self.url, "dir_info": {}}) if name == "direct_url.json" else None

    from importlib import metadata
    monkeypatch.setattr(metadata, "distribution", lambda name: _Dist(package.as_uri()))
    found = cli._installed_source_checkout()  # noqa: SLF001
    assert found["repository"] == str(repo.resolve()) or found["repository"] == str(repo)
    assert found["commit"] == head and found["reason"] == ""

    # An untracked build folder is not a change; an edit to a tracked file is.
    (package / "build").mkdir()
    (package / "build" / "x").write_text("x", encoding="utf-8")
    assert cli._installed_source_checkout()["reason"] == ""  # noqa: SLF001
    (package / "pyproject.toml").write_text("[project]\nname='y'\n", encoding="utf-8")
    assert cli._installed_source_checkout()["reason"] == "uncommitted_changes"  # noqa: SLF001

    # An index install records no file URL: not a checkout.
    monkeypatch.setattr(metadata, "distribution", lambda name: _Dist("https://pypi.org/x.whl"))
    assert cli._installed_source_checkout() is None  # noqa: SLF001


def test_a_snapshot_host_never_hands_procedure_install_to_its_old_snapshot(tmp_path, monkeypatch):
    """The reported mechanism: the bootstrap's procedure install ran the pinned snapshot's code."""

    config = tmp_path / "target" / "relay.json"
    old = SimpleNamespace(path=tmp_path / "releases" / "old", script=tmp_path / "releases" / "old" / "pb.py")
    monkeypatch.setattr(entrypoint, "effective_selection",
                        lambda root, release_source=None: {"mode": "snapshot", "commit": "a" * 40})
    monkeypatch.setattr(entrypoint, "selected_release", lambda root, selected, release_roots=(): old)
    installed = {"mode": "released", "version": NEW}

    assert entrypoint._selected_command(  # noqa: SLF001
        ["procedure", "install", "--target", "claude-code"], current_source=installed, config_path=config,
    ) is None
    # Every other command still runs in the host's selected snapshot until the switch.
    handed = entrypoint._selected_command(["status"], current_source=installed, config_path=config)  # noqa: SLF001
    assert handed is not None and str(old.script) in handed
