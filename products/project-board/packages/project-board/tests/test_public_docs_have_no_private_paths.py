"""The public procedures and documentation never point into a private repository (W340).

app-ecosystem is public as a whole, and the client package must be
self-contained: an agent with only the public client explains Problem Board
from public pages. A path, link or pull-request number into the private app
repository is something a public reader cannot open, so none may appear in the
procedures, in `docs/`, or in any product's `docs/`. A repository NAME used as
an example (a deploy-key label, a `--set-source-repo-url` placeholder) is not a
path into it and stays allowed.
"""

from __future__ import annotations

import re
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PACKAGE_ROOT.parents[3]
PROCEDURES = PACKAGE_ROOT / "src" / "project_board" / "procedures"

PRIVATE = (
    re.compile(r"playground/domain-solution"),
    re.compile(r"problem-board@1-0/(docs|services|ui|tests)\b"),
    re.compile(r"kdcube-docs/"),
    re.compile(r"github\.com/(kdcube|elenaviter)/applications(?!\.git\b)"),
    re.compile(r"repo:applications/"),
    re.compile(r"\bapplications ?#\d+"),
    re.compile(r"~/src/kdcube/applications"),
)


def _public_markdown() -> list[Path]:
    roots = [PROCEDURES, REPO_ROOT / "docs", *sorted((REPO_ROOT / "products").glob("*/docs"))]
    files: list[Path] = []
    for root in roots:
        if root.is_dir():
            files.extend(sorted(root.rglob("*.md")))
    return files


def test_the_scan_covers_the_procedures_and_every_public_docs_tree() -> None:
    files = {path.relative_to(REPO_ROOT).as_posix() for path in _public_markdown()}
    assert any(name.endswith("problem-board-worker/SKILL.md") for name in files)
    assert any(name.startswith("products/project-board/docs/") for name in files)
    assert any(name.startswith("docs/") for name in files)


def test_no_public_page_points_into_a_private_repository() -> None:
    found = []
    for path in _public_markdown():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            for pattern in PRIVATE:
                if pattern.search(line):
                    found.append(f"{path.relative_to(REPO_ROOT)}:{number}: {pattern.pattern}")
    assert not found, "private references in public pages:\n" + "\n".join(found)
