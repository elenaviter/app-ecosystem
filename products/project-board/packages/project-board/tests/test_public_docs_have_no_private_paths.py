"""The public procedures and documentation never point into a private repository,
and never name the project's people, organisations, hosts or agents (W340).

app-ecosystem is public as a whole, and the client package must be
self-contained: an agent with only the public client explains Problem Board
from public pages. A path, link or pull-request number into the private app
repository is something a public reader cannot open, so none may appear in the
procedures, in `docs/`, or in any product's `docs/`. A repository NAME used as
an example (a deploy-key label, a `--set-source-repo-url` placeholder) is not a
path into it and stays allowed.

Names are read from a private file, never from this public test: a list of
names or of their hashes here would publish them (a short guess list recovers
hashed names). `PB_PRIVATE_NAMES_FILE` points at the project's private list,
one name per line; each word of a page, and each run of its hyphen-joined
parts, is compared with it. Without the variable the name check skips; with
`PB_REQUIRE_PRIVATE_NAMES=1` it fails instead, so a maintainer run proves it
ran. The repository's own public address (its clone URL and its PyPI owner
line) is public by construction and is removed before the check. Machine
names are not listed: they open no door (operator, 2026-09-23).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PACKAGE_ROOT.parents[3]
PROCEDURES = PACKAGE_ROOT / "src" / "project_board" / "procedures"

PRIVATE = (
    re.compile(r"playground/domain-solution"),
    # Any folder of the private app, except the public MCP endpoint path.
    re.compile(r"problem-board@1-0/(?!public/)[A-Za-z_]"),
    re.compile(r"/home/[a-z_][a-z0-9_-]*/"),
    re.compile(r"/Users/[A-Za-z0-9_.-]+/"),
    re.compile(r"kdcube-docs/"),
    re.compile(r"github\.com/(kdcube|elenaviter)/applications(?!\.git\b)"),
    re.compile(r"repo:applications/"),
    re.compile(r"\bapplications ?#\d+"),
    re.compile(r"~/src/kdcube/applications"),
)

PRIVATE_NAMES_FILE = "PB_PRIVATE_NAMES_FILE"
REQUIRE_PRIVATE_NAMES = "PB_REQUIRE_PRIVATE_NAMES"


def _private_names() -> frozenset[str]:
    """The private names, lowercased, from the file the environment names."""

    location = os.environ.get(PRIVATE_NAMES_FILE, "").strip()
    if not location:
        if os.environ.get(REQUIRE_PRIVATE_NAMES, "").strip() == "1":
            pytest.fail(f"{PRIVATE_NAMES_FILE} is required and not set")
        pytest.skip(f"no private names list: set {PRIVATE_NAMES_FILE}")
    names = frozenset(
        line.strip().lower()
        for line in Path(location).expanduser().read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )
    assert names, f"{PRIVATE_NAMES_FILE} lists no names"
    return names

# This repository's own public address: its clone URL and the owner line of its
# PyPI trusted-publisher registration (docs/releases.md).
OWN_ADDRESS = (
    re.compile(r"github\.com/[A-Za-z0-9-]+/app-ecosystem\b"),
    re.compile(r"^owner: [A-Za-z0-9-]+$"),
)

WORD = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def _names_in(line: str, names: frozenset[str]) -> list[str]:
    for pattern in OWN_ADDRESS:
        line = pattern.sub(" ", line)
    found = []
    for word in WORD.findall(line.lower()):
        parts = word.split("-")
        for start in range(len(parts)):
            for end in range(start + 1, len(parts) + 1):
                candidate = "-".join(parts[start:end])
                if candidate in names:
                    found.append(candidate)
    return found


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


def test_no_public_page_names_a_private_person_or_organisation() -> None:
    names = _private_names()
    found = []
    for path in _public_markdown():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if _names_in(line, names):
                # The line number only: printing the name would publish it in CI logs.
                found.append(f"{path.relative_to(REPO_ROOT)}:{number}")
    assert not found, f"private names in public pages (see {PRIVATE_NAMES_FILE}):\n" + "\n".join(found)


def test_the_name_check_finds_a_listed_name_inside_an_alias_and_a_path() -> None:
    # A made-up list stands in for the private one, so this test names nobody.
    names = frozenset({"zq-probe", "zq"})
    assert _names_in("agent-zq-probe@host-two", names) == ["zq", "zq-probe"]
    assert _names_in("~/.kdcube/pb/workspaces/ZQ-Probe/applications", names) == ["zq", "zq-probe"]
    assert _names_in("zqx-prober and probe-zqx", names) == []
    # The placeholders the public pages use are plain words.
    assert _names_in("agent-one@host-two, my-agent, maintainer-host, agent-user", names) == []
    # The repository's own address is not a private name.
    assert _names_in("git clone https://github.com/zq-probe/app-ecosystem.git", names) == []


def test_without_the_private_list_the_check_skips_and_a_required_run_fails(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv(PRIVATE_NAMES_FILE, raising=False)
    monkeypatch.delenv(REQUIRE_PRIVATE_NAMES, raising=False)
    with pytest.raises(pytest.skip.Exception):
        _private_names()
    monkeypatch.setenv(REQUIRE_PRIVATE_NAMES, "1")
    with pytest.raises(pytest.fail.Exception):
        _private_names()
    listed = tmp_path / "names.txt"
    listed.write_text("# a comment\n\nZQ-Probe\n", encoding="utf-8")
    monkeypatch.setenv(PRIVATE_NAMES_FILE, str(listed))
    assert _private_names() == frozenset({"zq-probe"})
