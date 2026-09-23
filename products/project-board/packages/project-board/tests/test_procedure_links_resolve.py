"""Every link a procedure carries resolves on a host that has only this repository (W262).

Why: a worker host installs pb from the pinned App Ecosystem and KDCube
exports and holds no applications checkout. On 2026-09-23 the package
procedures still named about twenty repo:applications/... paths, dead on such
a host. The rule: a document in another repository is named by its public
address, a document in this repository by repo:app-ecosystem/<path>, and
everything the skill opens at run time ships inside the installed package.
"""

from __future__ import annotations

import re
from pathlib import Path

from project_board.client.procedures import install_agent_procedure, source_package_path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROCEDURES_ROOT = PACKAGE_ROOT / "src" / "project_board" / "procedures"
SKILL_ROOT = source_package_path()

_LINK = re.compile(r"\]\(([^)\s]+)\)")


def _repository_root() -> Path:
    current = PACKAGE_ROOT
    while current != current.parent:
        if (current / "products").is_dir() and (current / "packages").is_dir():
            return current
        current = current.parent
    raise AssertionError("the app-ecosystem repository root was not found above the package")


def _targets(path: Path) -> list[str]:
    """Markdown link targets plus the front matter's see_also entries."""

    text = path.read_text(encoding="utf-8")
    targets = list(_LINK.findall(text))
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        front = text[4:end] if end > 0 else ""
        in_see_also = False
        for line in front.splitlines():
            if line.startswith("see_also:"):
                in_see_also = True
                continue
            if in_see_also and line.startswith("  - "):
                targets.append(line[4:].strip())
            elif in_see_also and not line.startswith(" "):
                in_see_also = False
    return targets


def _resolves(target: str, *, document: Path, repository_root: Path | None) -> str:
    """'' when the target resolves, else the reason it does not."""

    if target.startswith(("http://", "https://", "mailto:")):
        return ""
    if target.startswith("#"):
        return ""
    if target.startswith("repo:"):
        if not target.startswith("repo:app-ecosystem/"):
            return "names another repository as a local path"
        if repository_root is None:
            return ""
        relative = target[len("repo:app-ecosystem/"):].split("#", 1)[0]
        return "" if (repository_root / relative).exists() else "not in this repository"
    relative = target.split("#", 1)[0]
    if not relative:
        return ""
    return "" if (document.parent / relative).exists() else "no such file beside the document"


def test_package_procedures_name_no_other_repository_as_a_local_path():
    root = _repository_root()
    dead: list[str] = []
    for document in sorted(PROCEDURES_ROOT.rglob("*.md")):
        for target in _targets(document):
            reason = _resolves(target, document=document, repository_root=root)
            if reason:
                dead.append(f"{document.relative_to(PROCEDURES_ROOT)}: {target} ({reason})")
    assert dead == [], "\n".join(dead)


def test_the_installed_skill_resolves_every_link_inside_its_own_tree(tmp_path):
    home = tmp_path / "home"
    installed = install_agent_procedure(["claude-code"], home=home)
    assert installed[0]["state"] == "installed"
    skills = [path for path in home.rglob("SKILL.md") if "_source" not in path.parts]
    assert len(skills) == 1, skills
    skill_root = skills[0].parent

    dead: list[str] = []
    for document in sorted(skill_root.rglob("*.md")):
        if "_source" in document.relative_to(skill_root).parts:
            continue
        for target in _targets(document):
            if target.startswith("repo:app-ecosystem/"):
                # An in-repository pointer is legitimate on a host with the
                # checkout; it is never what the skill needs at run time, so
                # it may appear only where the reader is told it is optional.
                continue
            reason = _resolves(target, document=document, repository_root=None)
            if not reason and not target.startswith(("http", "#")) and target:
                resolved = (document.parent / target.split("#", 1)[0]).resolve()
                if skill_root.resolve() not in resolved.parents and resolved != skill_root.resolve():
                    reason = "leaves the installed skill"
            if reason:
                dead.append(f"{document.relative_to(skill_root)}: {target} ({reason})")
    assert dead == [], "\n".join(dead)
    # The source of the rule: the installed tree carries no path into another repository.
    for document in skill_root.rglob("*.md"):
        assert "](repo:applications/" not in document.read_text(encoding="utf-8")
