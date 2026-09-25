"""Codex wake delivery resolves the executable installed by host setup."""

from __future__ import annotations

import os
from pathlib import Path

from project_board.client import session_delivery


def test_relay_finds_the_documented_user_node_codex_install(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "home"
    executable = home / ".local" / "node" / "bin" / "codex"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", str(tmp_path / "service-path-without-codex"))
    real_is_file = Path.is_file
    monkeypatch.setattr(
        Path,
        "is_file",
        lambda path: real_is_file(path) if path == executable else False,
    )

    assert session_delivery._codex_executable() == executable

    command_path = session_delivery._command_environment(executable)["PATH"]
    assert command_path.split(os.pathsep)[0] == str(executable.parent)
