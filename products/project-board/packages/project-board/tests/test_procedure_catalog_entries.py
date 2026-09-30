"""W417: an installed worker skill is one skill entry, however many releases it retains.

Skill catalogs that scan a skills directory for SKILL.md listed each retained
release's verbatim source copy (`_source/SKILL.md`) beside the active skill.
The copy is now `_source/SKILL.source.md`. A release installed before keeps the
legacy name, still verifies from its own manifest, and goes at the next prune.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from project_board.client import procedures
from project_board.client.procedures import install_agent_procedure, verify_agent_procedure


def _skill_entries(home: Path) -> list[Path]:
    return sorted(home.rglob("SKILL.md"))


def _newer_package(tmp_path: Path, revision: str) -> Path:
    root = tmp_path / f"package-{revision}"
    shutil.copytree(procedures.source_package_path(), root)
    manifest = root / "package.json"
    definition = json.loads(manifest.read_text(encoding="utf-8"))
    definition["revision"] = revision
    manifest.write_text(json.dumps(definition, indent=2) + "\n", encoding="utf-8")
    return root


def test_a_fresh_install_exposes_one_skill_entry_per_runtime(tmp_path):
    home = tmp_path / "home"
    installed = install_agent_procedure(["claude-code", "codex"], home=home)
    assert [item["state"] for item in installed] == ["installed", "installed"]

    entries = _skill_entries(home)
    assert len(entries) == 2, entries
    assert all(entry.parent.name == "problem-board-worker" for entry in entries)
    snapshots = sorted(home.rglob("SKILL.source.md"))
    assert len(snapshots) == 2 and all(path.parent.name == "_source" for path in snapshots)
    assert all(item["state"] == "current" for item in verify_agent_procedure(["claude-code", "codex"], home=home))


def test_a_legacy_snapshot_release_verifies_and_is_pruned_after_the_next_install(tmp_path, monkeypatch):
    home = tmp_path / "home"
    first_upgrade = _newer_package(tmp_path, "2999.01.01.1")
    second_upgrade = _newer_package(tmp_path, "2999.01.01.2")
    # A release written by an installer from before W417.
    monkeypatch.setattr(procedures, "INSTALLED_SOURCE_ENTRYPOINT", "_source/SKILL.md")
    install_agent_procedure(["claude-code"], home=home)
    monkeypatch.undo()
    assert verify_agent_procedure(["claude-code"], home=home)[0]["state"] == "current"
    assert len(_skill_entries(home)) == 2  # the active skill and the legacy copy

    # The upgrade writes the new name and keeps the legacy release as the previous one.
    monkeypatch.setattr(procedures, "source_package_path", lambda: first_upgrade)
    install_agent_procedure(["claude-code"], home=home)
    assert verify_agent_procedure(["claude-code"], home=home)[0]["state"] == "current"
    assert len(_skill_entries(home)) == 2

    # The next install prunes it: one skill entry remains.
    monkeypatch.setattr(procedures, "source_package_path", lambda: second_upgrade)
    install_agent_procedure(["claude-code"], home=home)
    assert verify_agent_procedure(["claude-code"], home=home)[0]["state"] == "current"
    entries = _skill_entries(home)
    assert len(entries) == 1 and entries[0].parent.name == "problem-board-worker", entries
