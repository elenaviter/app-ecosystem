"""A session enrolling a runtime checks that runtime's installed procedure (W304 U8).

On 2026-09-25 Codex was added to a host that had run only Claude Code. Its
first session started from an old copy of the worker skill, and nothing told
it: host checks covered only the runtimes the host already ran, and the
procedure text asking for an install of every runtime had been there all along.

`pb worker listen` now reads the installed copy for its own runtime (never
writing it) and, when that copy is not current, puts the one install command
first in `next`, with the re-read that must follow.
"""

from __future__ import annotations

from typing import Any

from project_board.client import cli
from project_board.client.procedures import install_agent_procedure, source_package
from test_attendance_materializes_project import _fresh_host


def _listen(identity) -> dict[str, Any]:
    return cli._worker_command(  # noqa: SLF001 - the command under test
        cli.build_parser().parse_args(
            [
                "worker", "listen",
                "--runtime-kind", identity.runtime_kind,
                "--runtime-session-id", identity.runtime_session_id,
            ]
        )
    )


def test_a_session_whose_runtime_skill_is_missing_is_told_to_install_it_first(tmp_path, monkeypatch):
    host, identity, _field, _config = _fresh_host(tmp_path)
    monkeypatch.setenv("PROBLEM_BOARD_CONFIG", str(host.path))
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    result = _listen(identity)

    procedure = result["procedure"]
    assert procedure["target"] == identity.runtime_kind
    assert procedure["state"] == "missing"
    assert procedure["install"] == ["pb", "procedure", "install", "--target", identity.runtime_kind]
    assert "re-read the installed SKILL.md" in procedure["rule"]
    assert next(iter(result["next"])) == "install_procedure"
    assert result["next"]["install_procedure"] == procedure["install"]
    # Listening is read-only for the procedure: nothing was written.
    assert not any(home.rglob("SKILL.md"))


def test_a_current_runtime_skill_needs_nothing(tmp_path, monkeypatch):
    host, identity, _field, _config = _fresh_host(tmp_path)
    monkeypatch.setenv("PROBLEM_BOARD_CONFIG", str(host.path))
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    install_agent_procedure([identity.runtime_kind], home=home)

    result = _listen(identity)

    assert result["procedure"]["state"] == "current"
    assert result["procedure"]["installed_revision"] == source_package()["revision"]
    assert "install" not in result["procedure"]
    assert "install_procedure" not in result["next"]
