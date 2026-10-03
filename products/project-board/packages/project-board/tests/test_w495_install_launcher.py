"""W495: the first `pb procedure install` gives a fresh host one `pb` command.

On 2026-10-03 a colleague installed pb from source into the bootstrap venv,
ran `pb procedure install`, and their agent could not run `pb`: the only pb was
inside the venv, and an older pb elsewhere on PATH answered instead. The
install now writes the marked launcher at ~/.local/bin/pb for the pb that ran
it, says when ~/.local/bin is not on PATH or another pb shadows it, and leaves
a launcher that a source selection owns, or an unrelated command, alone.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

from project_board.client import cli
from project_board.client.release_install import (
    LAUNCHER_MARKER,
    ensure_install_launcher,
    install_launcher,
    validate_launcher,
)


def _executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_a_fresh_host_gets_the_launcher_for_the_installing_pb(tmp_path):
    pb = _executable(tmp_path / "bootstrap" / "bin" / "pb")
    home = tmp_path / "home"
    local_bin = home / ".local" / "bin"

    result = ensure_install_launcher(home, pb=pb, path_env=str(local_bin), shell="/bin/zsh")

    launcher = local_bin / "pb"
    assert result["state"] == "installed" and result["path"] == str(launcher)
    text = launcher.read_text(encoding="utf-8")
    assert LAUNCHER_MARKER in text and str(pb) in text
    assert os.access(launcher, os.X_OK)
    assert result["on_path"] is True and "add_to_path" not in result


def test_a_second_install_changes_nothing(tmp_path):
    pb = _executable(tmp_path / "bootstrap" / "bin" / "pb")
    home = tmp_path / "home"
    first = ensure_install_launcher(home, pb=pb, path_env=str(home / ".local" / "bin"))
    before = (home / ".local" / "bin" / "pb").read_bytes()

    again = ensure_install_launcher(home, pb=pb, path_env=str(home / ".local" / "bin"))

    assert first["state"] == "installed" and again["state"] == "current"
    assert (home / ".local" / "bin" / "pb").read_bytes() == before


def test_a_source_selection_replaces_the_install_launcher_without_force(tmp_path):
    """The point of the marked launcher: setup's use-code/use-release owns it."""

    bootstrap = _executable(tmp_path / "bootstrap" / "bin" / "pb")
    release = _executable(tmp_path / "releases" / "abc" / "venv" / "bin" / "pb")
    home = tmp_path / "home"
    ensure_install_launcher(home, pb=bootstrap, path_env=str(home / ".local" / "bin"))
    launcher = home / ".local" / "bin" / "pb"

    assert validate_launcher(launcher, expected_pb=release) == launcher
    install_launcher(launcher, expected_pb=release)
    assert str(release) in launcher.read_text(encoding="utf-8")


def test_a_launcher_for_the_selected_release_is_left_alone(tmp_path):
    """On a configured host the bootstrap install never repoints the managed launcher."""

    release = _executable(tmp_path / "releases" / "current" / "venv" / "bin" / "pb")
    bootstrap = _executable(tmp_path / "bootstrap" / "bin" / "pb")
    home = tmp_path / "home"
    launcher = install_launcher(home / ".local" / "bin" / "pb", expected_pb=release)
    before = launcher.read_bytes()

    result = ensure_install_launcher(home, pb=bootstrap, path_env=str(launcher.parent))

    assert result["state"] == "selected"
    assert launcher.read_bytes() == before
    assert "use-code" in result["note"]


def test_an_unrelated_pb_is_named_and_left_alone(tmp_path):
    bootstrap = _executable(tmp_path / "bootstrap" / "bin" / "pb")
    home = tmp_path / "home"
    other = _executable(home / ".local" / "bin" / "pb")
    before = other.read_bytes()

    result = ensure_install_launcher(home, pb=bootstrap, path_env=str(other.parent))

    assert result["state"] == "other_command"
    assert other.read_bytes() == before
    assert str(bootstrap) in result["note"] and "Remove or rename it" in result["note"]


def test_a_missing_path_entry_gets_the_line_for_the_user_s_shell(tmp_path):
    pb = _executable(tmp_path / "bootstrap" / "bin" / "pb")
    home = tmp_path / "home"

    zsh = ensure_install_launcher(home, pb=pb, path_env=str(tmp_path / "usr" / "bin"), shell="/usr/bin/zsh")
    bash = ensure_install_launcher(home, pb=pb, path_env=str(tmp_path / "usr" / "bin"), shell="/bin/bash")

    assert zsh["on_path"] is False
    assert zsh["add_to_path"] == "echo 'export PATH=\"$HOME/.local/bin:$PATH\"' >> ~/.zshrc"
    assert bash["add_to_path"].endswith(">> ~/.bashrc")
    assert "open a new terminal and start your agent from it" in zsh["note"]


def test_an_older_pb_earlier_on_path_is_named(tmp_path):
    pb = _executable(tmp_path / "bootstrap" / "bin" / "pb")
    old = _executable(tmp_path / "old" / "bin" / "pb")
    home = tmp_path / "home"
    path_env = os.pathsep.join([str(old.parent), str(home / ".local" / "bin")])

    result = ensure_install_launcher(home, pb=pb, path_env=path_env)

    assert result["state"] == "installed"
    assert result["shadowed_by"] == str(old)
    assert f"`pb` on PATH runs {old}" in result["note"]


def test_procedure_install_reports_the_launcher(tmp_path, monkeypatch):
    running = _executable(tmp_path / "bootstrap" / "bin" / "pb")
    monkeypatch.setattr(sys, "argv", [str(running), "procedure", "install"])
    home = tmp_path / "home"
    monkeypatch.setenv("PATH", str(home / ".local" / "bin"))

    result = cli._procedure_command(  # noqa: SLF001 - the command under test
        SimpleNamespace(procedure_command="install", target=["codex"], home=str(home), force=False,
                        allow_downgrade=False, config=None)
    )

    assert result["launcher"]["state"] == "installed"
    assert str(running) in (home / ".local" / "bin" / "pb").read_text(encoding="utf-8")
