"""pb procedure verify names the files a new revision changed (W563).

Why: after a revision change a coordinator reread the whole package, about
36,000 words, although one or two references had changed. Verify now names
the package files whose digest differs from the generation the install
replaced, and that generation's revision, so a session that loaded it rereads
only those files in full.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from project_board.client import procedures
from project_board.client.procedures import install_agent_procedure, verify_agent_procedure


def _stand_in_package(tmp_path: Path, revision: str, *, edit: str = "") -> Path:
    root = tmp_path / f"package-{revision}"
    shutil.copytree(procedures.source_package_path(), root)
    manifest = root / "package.json"
    definition = json.loads(manifest.read_text(encoding="utf-8"))
    definition["revision"] = revision
    manifest.write_text(json.dumps(definition, indent=2) + "\n", encoding="utf-8")
    if edit:
        target = root / edit
        target.write_text(target.read_text(encoding="utf-8") + "\nA changed line.\n", encoding="utf-8")
    return root


def test_a_first_install_names_no_changed_files(tmp_path):
    home = tmp_path / "home"
    install_agent_procedure(["claude-code"], home=home)

    verified = verify_agent_procedure(["claude-code"], home=home)[0]

    assert verified["state"] == "current"
    assert verified["changed_files"] == []
    assert verified["changed_since_revision"] == ""


def test_a_newer_revision_names_exactly_the_edited_reference_and_the_replaced_revision(tmp_path, monkeypatch):
    home = tmp_path / "home"
    install_agent_procedure(["claude-code"], home=home)
    replaced = procedures.source_package()["revision"]
    newer = _stand_in_package(tmp_path, "2099.01.01.1", edit="references/brief-output.md")
    monkeypatch.setattr(procedures, "source_package_path", lambda: newer)
    install_agent_procedure(["claude-code"], home=home)

    verified = verify_agent_procedure(["claude-code"], home=home)[0]

    assert verified["installed_revision"] == "2099.01.01.1"
    assert verified["changed_since_revision"] == replaced
    assert verified["changed_files"] == ["references/brief-output.md"]


def test_a_one_rule_change_names_only_that_rule_s_module(tmp_path, monkeypatch):
    # W563, coordinator 2026-10-05 22:51 UTC: the .8 -> .9 change made a
    # coordinator reread collaboration.md and coordinator.md whole, 183,315
    # bytes, for two new paragraphs. The references are split into modules,
    # so a one-rule change names one module and its size is the reload cost.
    home = tmp_path / "home"
    install_agent_procedure(["claude-code"], home=home)
    rule = "references/collaboration/rule-6-your-visible-state-says-where-you-are-and-what-you-ar.md"
    newer = _stand_in_package(tmp_path, "2099.01.01.2", edit=rule)
    monkeypatch.setattr(procedures, "source_package_path", lambda: newer)

    install_agent_procedure(["claude-code"], home=home)
    verified = verify_agent_procedure(["claude-code"], home=home)[0]

    assert verified["changed_files"] == [rule]
    module_bytes = len((newer / rule).read_bytes())
    whole_bytes = sum(
        len(path.read_bytes())
        for path in [newer / "references" / "collaboration.md", *(newer / "references" / "collaboration").glob("*.md")]
    )
    assert module_bytes < whole_bytes / 3
