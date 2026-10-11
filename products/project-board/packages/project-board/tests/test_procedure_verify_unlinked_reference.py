"""Verification reports an unlinked installed reference as package corruption."""

from __future__ import annotations

import json
import shutil

from project_board.client import procedures


def test_verify_reports_unlinked_installed_reference(tmp_path, monkeypatch):
    package_root = tmp_path / "source-package"
    shutil.copytree(procedures.source_package_path(), package_root)
    orphan = "references/unlinked.md"
    (package_root / orphan).write_text("Unlinked reference.\n", encoding="utf-8")
    manifest_path = package_root / "package.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["references"].append(orphan)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(procedures, "source_package_path", lambda: package_root)

    # Model a preexisting release whose installer accepted the unlinked file.
    # Its snapshot, manifest, and digests are otherwise produced by the real
    # installer, so verification must inspect the installed package itself.
    render = procedures._render_entrypoint

    def render_during_install(package, release):
        return render({**package, "references": package["references"][:-1]}, release)

    with monkeypatch.context() as install_patch:
        install_patch.setattr(procedures, "_render_entrypoint", render_during_install)
        procedures.install_agent_procedure(["codex"], home=tmp_path / "home")

    verified = procedures.verify_agent_procedure(["codex"], home=tmp_path / "home")[0]

    assert verified["state"] == "corrupt"
    assert verified["errors"] == [
        "The installed SKILL.md cannot be reproduced from its source snapshot: "
        "Every declared worker procedure reference must be linked from SKILL.md "
        "or from another declared reference."
    ]
