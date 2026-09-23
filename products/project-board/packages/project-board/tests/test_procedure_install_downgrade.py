"""pb procedure install refuses to put an older revision over a newer one.

Why: a host cutover installs from whatever the package carries. On 2026-09-23
both dev-main runtimes ran 2026.09.23.1 while the package still carried
2026.09.22.13, and the cutover would have downgraded every session silently,
with the install reporting success and the copy verifying as current against
the package it came from. The refusal names both revisions and leaves the
installed copy untouched. The deliberate case passes --allow-downgrade.

The stand-in packages are real: a copy of the source package with only the
manifest revision changed, read through the installer's own reader, so their
digests follow the installer's rule and the staged release verifies.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from project_board.client import procedures
from project_board.client.procedures import install_agent_procedure
from project_board.contract.errors import DomainError


def _stand_in_package(tmp_path: Path, revision: str) -> Path:
    root = tmp_path / f"package-{revision}"
    shutil.copytree(procedures.source_package_path(), root)
    manifest = root / "package.json"
    definition = json.loads(manifest.read_text(encoding="utf-8"))
    definition["revision"] = revision
    manifest.write_text(json.dumps(definition, indent=2) + "\n", encoding="utf-8")
    return root


def _select_package(monkeypatch, root: Path) -> None:
    monkeypatch.setattr(procedures, "source_package_path", lambda: root)


def test_an_older_package_is_refused_with_both_revisions_named(tmp_path, monkeypatch):
    home = tmp_path / "home"
    installed = install_agent_procedure(["claude-code"], home=home)
    assert installed[0]["state"] == "installed"
    current_revision = procedures.source_package()["revision"]

    _select_package(monkeypatch, _stand_in_package(tmp_path, "2026.09.22.13"))
    with pytest.raises(DomainError) as refused:
        install_agent_procedure(["claude-code"], home=home)
    assert refused.value.code == "work_agent_procedure_downgrade"
    assert refused.value.details["installed_revision"] == current_revision
    assert refused.value.details["package_revision"] == "2026.09.22.13"

    # The installed copy is untouched: it still verifies as the newer revision.
    monkeypatch.undo()
    verified = procedures.verify_agent_procedure(["claude-code"], home=home)
    assert verified[0]["installed_revision"] == current_revision
    assert verified[0]["state"] == "current"


def test_allow_downgrade_installs_the_older_package_on_purpose(tmp_path, monkeypatch):
    home = tmp_path / "home"
    install_agent_procedure(["claude-code"], home=home)
    _select_package(monkeypatch, _stand_in_package(tmp_path, "2026.09.22.13"))
    installed = install_agent_procedure(
        ["claude-code"], home=home, allow_downgrade=True
    )
    assert installed[0]["state"] == "installed"
    assert installed[0]["installed_revision"] == "2026.09.22.13"


def test_a_newer_package_installs_without_the_flag(tmp_path, monkeypatch):
    home = tmp_path / "home"
    install_agent_procedure(["claude-code"], home=home)
    _select_package(monkeypatch, _stand_in_package(tmp_path, "2099.01.01.1"))
    installed = install_agent_procedure(["claude-code"], home=home)
    assert installed[0]["state"] == "installed"
    assert installed[0]["installed_revision"] == "2099.01.01.1"


def test_non_numeric_revisions_are_not_ordered_and_never_refused(tmp_path, monkeypatch):
    home = tmp_path / "home"
    install_agent_procedure(["claude-code"], home=home)
    _select_package(monkeypatch, _stand_in_package(tmp_path, "rc-final"))
    installed = install_agent_procedure(["claude-code"], home=home)
    assert installed[0]["installed_revision"] == "rc-final"
