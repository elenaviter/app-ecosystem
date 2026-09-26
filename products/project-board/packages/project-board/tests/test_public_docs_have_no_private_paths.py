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

One door is never written anywhere, not even in the private list: the host of
this machine's selected Problem Board endpoint (a tunnel hostname). The check
reads it at test time from the `pb` host configuration and refuses it in every
public page; a failure prints only `file:line`.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

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
        # A name may carry a trailing `# note`; underscores count as hyphens.
        line.split("#", 1)[0].strip().lower().replace("_", "-")
        for line in Path(location).expanduser().read_text(encoding="utf-8").splitlines()
        if line.split("#", 1)[0].strip()
    )
    assert names, f"{PRIVATE_NAMES_FILE} lists no names"
    return names

# This repository's own public address: its clone URL and the owner line of its
# PyPI trusted-publisher registration (docs/releases.md).
OWN_ADDRESS = (
    re.compile(r"github\.com/[A-Za-z0-9-]+/app-ecosystem\b"),
    re.compile(r"^owner: [A-Za-z0-9-]+$"),
)

# Attribution the license requires, kept on purpose: each entry is one line
# shape and why it may name a person (W340 P4a2, coordinator 2026-09-26).
ATTRIBUTION = (
    # The copyright holder in a source header and in LICENSE: the license
    # names the holder, and removing it would change the license.
    re.compile(r"^\s*(?:#|//|\*)?\s*Copyright \(c\) \d{4}(?:-\d{4})? .+$"),
    # A package's author in pyproject.toml: the published distribution names it.
    re.compile(r"^authors = \[\{ name = \"[^\"]+\" \}\]$"),
)

NOT_A_DOOR = frozenset({"localhost", "127.0.0.1", "::1"})


def _endpoint_host() -> str:
    """The host of this machine's selected endpoint, or "" when there is none."""

    try:
        from project_board.client.host_config import HostRelayConfig, resolve_host_config_path

        endpoint = HostRelayConfig.load(resolve_host_config_path()).endpoint
    except Exception:  # noqa: BLE001 - no host configuration on this machine
        return ""
    host = (urlsplit(endpoint).hostname or "").lower()
    return "" if host in NOT_A_DOOR else host


WORD = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def _names_in(line: str, names: frozenset[str]) -> list[str]:
    for pattern in (*OWN_ADDRESS, *ATTRIBUTION):
        line = pattern.sub(" ", line)
    found = []
    # `demo_project` is the same name as `demo-project`.
    for word in WORD.findall(line.lower().replace("_", "-")):
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


def _tracked_text_files() -> list[Path]:
    """Every file git tracks in this repository that reads as UTF-8 text (W340 P4a2)."""

    try:
        listed = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "ls-files", "-z"],
            capture_output=True, check=True, timeout=60,
        ).stdout.decode("utf-8").split("\0")
    except (OSError, subprocess.SubprocessError):
        pytest.skip("not a git checkout: the tracked files cannot be listed")
    files = []
    for name in listed:
        path = REPO_ROOT / name
        if name and path.is_file() and not path.is_symlink():
            files.append(path)
    return files


def _lines_where(matches, files: list[Path]) -> list[str]:
    """`file:line` for every line ``matches`` accepts; the text itself is never kept.

    The scan runs here so a failing test holds only locations: with
    `--showlocals` a test's locals are printed, and they must not carry a
    matched line or the name in it.
    """

    found = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if matches(line):
                found.append(f"{path.relative_to(REPO_ROOT)}:{number}")
    return found


def _lines_naming_private_names() -> list[str]:
    names = _private_names()
    return _lines_where(lambda line: bool(_names_in(line, names)), _tracked_text_files())


def _lines_naming_this_hosts_endpoint() -> list[str] | None:
    host = _endpoint_host()
    if not host:
        return None
    return _lines_where(lambda line: host in line.lower(), _tracked_text_files())


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


def test_no_tracked_file_names_a_private_person_or_organisation() -> None:
    found = _lines_naming_private_names()
    assert not found, f"private names in tracked files (see {PRIVATE_NAMES_FILE}):\n" + "\n".join(found)


def test_the_name_check_finds_a_listed_name_inside_an_alias_and_a_path() -> None:
    # A made-up list stands in for the private one, so this test names nobody.
    names = frozenset({"zq-probe", "zq"})
    assert _names_in("agent-zq-probe@host-two", names) == ["zq", "zq-probe"]
    assert _names_in("~/.kdcube/pb/workspaces/ZQ-Probe/applications", names) == ["zq", "zq-probe"]
    assert _names_in("zqx-prober and probe-zqx", names) == []
    assert _names_in("the zq_probe folder", names) == ["zq", "zq-probe"]
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
    listed.write_text("# a comment\n\nZQ-Probe\nzq_other   # a note\n", encoding="utf-8")
    monkeypatch.setenv(PRIVATE_NAMES_FILE, str(listed))
    assert _private_names() == frozenset({"zq-probe", "zq-other"})


def test_no_tracked_file_names_this_hosts_endpoint() -> None:
    found = _lines_naming_this_hosts_endpoint()
    if found is None:
        if os.environ.get(REQUIRE_PRIVATE_NAMES, "").strip() == "1":
            pytest.fail("no Problem Board host configuration to read the endpoint from")
        pytest.skip("no Problem Board host configuration on this machine")
    assert not found, "this host's endpoint appears in tracked files:\n" + "\n".join(found)


def test_the_endpoint_probe_reads_the_host_configuration(monkeypatch, tmp_path) -> None:
    from project_board.client import host_config

    class Loaded:
        endpoint = "https://door.example.invalid/mcp/problem-board"

    monkeypatch.setattr(host_config, "resolve_host_config_path", lambda *_: tmp_path / "config.json")
    monkeypatch.setattr(host_config.HostRelayConfig, "load", classmethod(lambda cls, _path: Loaded()))
    assert _endpoint_host() == "door.example.invalid"
    Loaded.endpoint = "http://localhost:8080/mcp"
    assert _endpoint_host() == ""

    def unconfigured(*_):
        raise RuntimeError("no configuration")

    monkeypatch.setattr(host_config, "resolve_host_config_path", unconfigured)
    assert _endpoint_host() == ""


def test_attribution_lines_are_the_only_named_exception() -> None:
    names = frozenset({"zq-probe"})
    assert _names_in("# Copyright (c) 2026 Zq-Probe", names) == []
    assert _names_in("Copyright (c) 2025-2026 Zq-Probe", names) == []
    assert _names_in('authors = [{ name = "Zq-Probe" }]', names) == []
    # The same name anywhere else is found.
    assert _names_in("# maintained by Zq-Probe", names) == ["zq-probe"]
    assert _names_in('owner = "zq-probe"  # Copyright (c) 2026', names) == ["zq-probe"]


def test_the_scan_reads_every_tracked_text_file() -> None:
    files = {path.relative_to(REPO_ROOT).as_posix() for path in _tracked_text_files()}
    assert "LICENSE" in files
    assert any(name.endswith(".py") and "/tests/" in name for name in files)
    assert any(name.endswith("pyproject.toml") for name in files)
    assert not any("/node_modules/" in name for name in files)
