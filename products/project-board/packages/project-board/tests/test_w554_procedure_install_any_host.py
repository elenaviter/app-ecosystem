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


def test_the_installing_release_becomes_the_selection_and_the_launcher_follows(tmp_path, monkeypatch):
    config = _host(tmp_path, monkeypatch, relay_source.released_selection(OLD))
    home = tmp_path / "home"
    old_release = _executable(tmp_path / "releases" / "old" / "venv" / "bin" / "pb")
    install_launcher(home / ".local" / "bin" / "pb", expected_pb=old_release)
    running = _executable(tmp_path / "bootstrap" / "bin" / "pb")
    monkeypatch.setattr(sys, "argv", [str(running), "procedure", "install"])
    monkeypatch.setenv("PATH", str(home / ".local" / "bin"))

    result = cli._procedure_command(  # noqa: SLF001 - the command under test
        SimpleNamespace(procedure_command="install", target=["codex"], home=str(home), force=False,
                        allow_downgrade=False, config=None)
    )

    assert result["selection"]["adopted"] is True
    assert result["selection"]["previous_version"] == relay_source.canonical_release_version(OLD)
    selected = relay_source.read_selection(relay_source.client_source_root(config))
    assert source_control.source_matches({"mode": "released", "version": NEW}, selected)
    launcher = (home / ".local" / "bin" / "pb").read_text(encoding="utf-8")
    assert str(running) in launcher and str(old_release) not in launcher
    assert result["launcher"]["state"] == "current"
    # The next command through the bootstrap now runs without a refusal.
    assert entrypoint._selected_command(
        ["status"], current_source={"mode": "released", "version": NEW}, config_path=config
    ) is None


def test_a_matching_selection_and_a_new_host_change_nothing(tmp_path, monkeypatch):
    config = _host(tmp_path, monkeypatch, relay_source.released_selection(NEW))
    before = relay_source.read_selection(relay_source.client_source_root(config))

    assert cli._adopt_installing_release() is None  # noqa: SLF001
    assert relay_source.read_selection(relay_source.client_source_root(config)) == before

    monkeypatch.setenv("PROBLEM_BOARD_CONFIG", "")
    monkeypatch.setenv("HOME", str(tmp_path / "fresh"))
    assert cli._adopt_installing_release() is None  # noqa: SLF001 - no configuration yet


def test_a_host_that_selected_a_source_snapshot_keeps_it(tmp_path, monkeypatch):
    config = tmp_path / "target" / "relay.json"
    monkeypatch.setenv("PROBLEM_BOARD_CONFIG", str(config))
    root = relay_source.client_source_root(config)
    root.mkdir(parents=True)
    # Only the selection's mode decides here; a snapshot record as use-code writes it.
    (root / relay_source.SELECTION_FILE).write_text(
        '{"schema": "%s", "mode": "snapshot", "commit": "%s"}' % (relay_source.LEGACY_SELECTION_SCHEMA, "a" * 40),
        encoding="utf-8",
    )
    monkeypatch.setattr(source_control, "installed_release_source", lambda: {"mode": "released", "version": NEW})

    before = (root / relay_source.SELECTION_FILE).read_bytes()

    assert cli._adopt_installing_release() is None  # noqa: SLF001
    assert (root / relay_source.SELECTION_FILE).read_bytes() == before



def test_an_older_installed_release_is_adopted_too(tmp_path, monkeypatch):
    """Review P3: the person chose the installed package; only an older procedure is refused."""

    config = _host(tmp_path, monkeypatch, relay_source.released_selection("2026.11.1.100"))

    adopted = cli._adopt_installing_release()  # noqa: SLF001

    assert adopted and adopted["version"] == relay_source.canonical_release_version(NEW)
    selected = relay_source.read_selection(relay_source.client_source_root(config))
    assert source_control.source_matches({"mode": "released", "version": NEW}, selected)
