"""`pb source versions`: what is published, what is installed here, what runs now (W322).

The operator agreed (2026-09-26 ~23:10Z) that the PyPI package is the loader
and `pb source use-release` installs an exact version. Updating and rolling
back then need one read-only answer: the published versions from the package
index, which of them this machine has installed and which is active, and the
exact use-release line for the chosen one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from project_board.client import cli
from project_board.client.release_install import (
    INSTALLATION_MARKER,
    INSTALLATION_SCHEMA,
    published_release_id,
    releases_path,
)
from project_board.client.source_versions import source_versions
from project_board.contract.errors import DomainError

INDEX = "https://index.example/simple"


def _fake_index(versions, yanked=()):
    calls = []

    def fetch(url):
        calls.append(url)
        files = []
        for version in versions:
            files.append({"filename": f"project_board-{version}-py3-none-any.whl", "yanked": version in yanked})
            files.append({"filename": f"project_board-{version}.tar.gz", "yanked": version in yanked})
        return {"meta": {"api-version": "1.1"}, "name": "project-board", "versions": list(versions), "files": files}

    fetch.calls = calls
    return fetch


def _install(root: Path, version: str, *, active: bool = False, mode: str = "released") -> str:
    release_id = published_release_id(version)
    release = releases_path(root) / release_id
    (release / "venv" / "bin").mkdir(parents=True)
    for name in ("python", "pb"):
        (release / "venv" / "bin" / name).write_text("", encoding="utf-8")
    source = {"mode": mode, "version": version} if mode == "released" else {"mode": mode}
    (release / INSTALLATION_MARKER).write_text(json.dumps({
        "schema": INSTALLATION_SCHEMA,
        "release_id": release_id,
        "source": source,
        "environment": {"python": "3.11"},
    }), encoding="utf-8")
    if active:
        (releases_path(root) / "current").symlink_to(release_id)
    return release_id


def test_it_lists_the_index_newest_first_and_marks_installed_and_active(tmp_path):
    fetch = _fake_index(["2026.9.22.2100", "2026.9.27.130", "2026.9.26.2220"])
    _install(tmp_path, "2026.9.22.2100")
    _install(tmp_path, "2026.9.26.2220", active=True)

    result = source_versions(tmp_path, fetch=fetch, index=INDEX)

    assert fetch.calls == [f"{INDEX}/project-board/"]
    assert [(row["version"], row["installed"], row["active"]) for row in result["versions"]] == [
        ("2026.9.27.130", False, False),
        ("2026.9.26.2220", True, True),
        ("2026.9.22.2100", True, False),
    ]
    assert result["latest"] == "2026.9.27.130"
    assert result["active"] == {"mode": "released", "version": "2026.9.26.2220"}
    # The newest is chosen by default: the update line.
    assert result["use_release"] == "pb source use-release --expect-version 2026.9.27.130"


def test_a_chosen_version_prints_its_line_and_the_active_one_says_nothing_to_change(tmp_path):
    fetch = _fake_index(["2026.9.26.2220", "2026.9.27.130"])
    _install(tmp_path, "2026.9.27.130", active=True)

    back = source_versions(tmp_path, choose="2026.09.26.2220", fetch=fetch, index=INDEX)
    assert back["chosen"] == "2026.9.26.2220"
    assert back["use_release"] == "pb source use-release --expect-version 2026.9.26.2220"

    same = source_versions(tmp_path, fetch=fetch, index=INDEX)
    assert same["chosen"] == "2026.9.27.130" and same["note"] == "Already active; nothing to change."


def test_an_unpublished_or_invalid_choice_is_refused_by_name(tmp_path):
    fetch = _fake_index(["2026.9.26.2220"])
    with pytest.raises(DomainError) as missing:
        source_versions(tmp_path, choose="2026.9.28.100", fetch=fetch, index=INDEX)
    assert missing.value.code == "work_client_version_not_published"
    with pytest.raises(DomainError) as invalid:
        source_versions(tmp_path, choose="latest-please", fetch=fetch, index=INDEX)
    assert invalid.value.code == "work_client_version_invalid"


def test_a_yanked_version_is_listed_but_never_chosen_by_default(tmp_path):
    fetch = _fake_index(["2026.9.26.2220", "2026.9.27.130"], yanked={"2026.9.27.130"})
    result = source_versions(tmp_path, fetch=fetch, index=INDEX)
    assert [row["yanked"] for row in result["versions"]] == [True, False]
    assert result["latest"] == "2026.9.26.2220"
    assert result["use_release"].endswith("2026.9.26.2220")


def test_a_code_snapshot_is_active_but_no_version_is(tmp_path):
    fetch = _fake_index(["2026.9.26.2220"])
    snapshot = _install(tmp_path, "0", active=True, mode="snapshot")
    result = source_versions(tmp_path, fetch=fetch, index=INDEX)
    assert result["active"] == {"mode": "snapshot", "release_id": snapshot}
    assert not any(row["active"] or row["installed"] for row in result["versions"])


def test_the_command_is_read_only_and_works_before_setup(tmp_path, monkeypatch):
    # No machine configuration: the default release store is read.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("PROBLEM_BOARD_CONFIG", raising=False)
    seen = {}

    def fake(root, *, choose, index):
        seen.update(root=root, choose=choose, index=index)
        return {"schema": "project-board.client-source-versions.v1"}

    monkeypatch.setattr("project_board.client.source_versions.source_versions", fake)
    args = cli.build_parser().parse_args(["source", "versions", "--version", "2026.9.26.2220", "--index-url", INDEX])
    monkeypatch.setattr(cli, "resolve_host_config_path", lambda value=None: (_ for _ in ()).throw(
        DomainError("work_relay_config_required", "no config")))
    assert cli._source_command(args)["schema"] == "project-board.client-source-versions.v1"
    assert str(seen["root"]).endswith(".kdcube/client-runtime/tools/problem-board")
    assert seen["choose"] == "2026.9.26.2220" and seen["index"] == INDEX
