"""The public procedures and documentation never point into a private repository,
and never name the project's people, organisations, hosts or agents (W340).

app-ecosystem is public as a whole, and the client package must be
self-contained: an agent with only the public client explains Problem Board
from public pages. A path, link or pull-request number into the private app
repository is something a public reader cannot open, so none may appear in the
procedures, in `docs/`, or in any product's `docs/`. A repository NAME used as
an example (a deploy-key label, a `--set-source-repo-url` placeholder) is not a
path into it and stays allowed.

Names are checked by hash: this public test must not name them either. Each
word of a page (and each run of its hyphen-joined parts) is hashed and compared
with the hashes of the private names. To add a name, add
`hashlib.sha256(name.lower().encode()).hexdigest()`. The repository's own public
address (its clone URL and its PyPI owner line) is public by construction and is
removed before the check.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

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

PRIVATE_NAME_HASHES = frozenset({
    "94364d42fabb0be8e99f509a9a62f84c3a8c71b6b4d2da52b1c15a5473ba364b",
    "03cdb5536dc3150fd8dbcbc2ea27776a32005ccbc621f88563f90e2ce1a4e61f",
    "cc967443070ab409a57a455dc8c2405a10b6ba96ca75f87bcf18ca368bb090a8",
    "0ce93c9606f0685bf60e051265891d256381f639d05c0aec67c84eec49d33cc1",
    "795e55c568cee7455748c5ae48953bec0ccf9229fe0e85f07945960fef0ce627",
    "635d3933acbae41a13ecd20ff8e5a936debc94f2c0d995183509cb5da2aad579",
    "2a6d847da3ef4d51e1228347e35c7f784560659592e623091f8ea44db714a175",
    "0df89317e02535902d116be0f27294a75145339bf4af53fb35131aea8071a0e1",
    "50ef43677caa6aab199caefb3883415e6f515a535e867f0fa09e0f5c752d3b5a",
    "2e2907386a93de1dd6000948948aebccaabe012b492ee894f2b091a48ce6691c",
    "1db1a1868b097ee3d3b8a485549097d4e7bf0cee5fe8c1997195df2f4046835a",
    "224b4f95bbe48b40649515fac9dad3f0e6693fefb4689f561f846e2119d3d75b",
    "c2e9644f996a6475ee5e4a5a644f6ce5f838394aed710b16fbce7056a1483eab",
    "177d78514028e81e1f90e35569d939b67ce735f510dae387967df52c837af225",
    "315dc1c3a775edd6cbb6f413fe27f163f441a5574ee8df2b038fa40343f828fa",
    "16170e88695f8e82bec30a377f0587e4010a93e4cb49b1a51f597b04a3956323",
    "f02c63b09978720996db5b5a8cbd2d41dbf4457cacfd8375c087bcec2164a103",
    "9b0945d898182d5473d4326c25beea69aee5d477ded321467ea5e1bfa40c96f9",
    "d26f44459530e6aa95e159354c3f5ba59977c3273c8ce17736f28949ed341042",
    "bf0374b06424e71aba097e8955a21fb961e24b8eb2f3b85def136b011415f8fa",
    "55221fad19df58af459dd217017dde7d94cb5482aa7cb5bebeb0744d898f300d",
})

# This repository's own public address: its clone URL and the owner line of its
# PyPI trusted-publisher registration (docs/releases.md).
OWN_ADDRESS = (
    re.compile(r"github\.com/[A-Za-z0-9-]+/app-ecosystem\b"),
    re.compile(r"^owner: [A-Za-z0-9-]+$"),
)

WORD = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def _names_in(line: str) -> list[str]:
    for pattern in OWN_ADDRESS:
        line = pattern.sub(" ", line)
    found = []
    for word in WORD.findall(line.lower()):
        parts = word.split("-")
        for start in range(len(parts)):
            for end in range(start + 1, len(parts) + 1):
                candidate = "-".join(parts[start:end])
                if hashlib.sha256(candidate.encode()).hexdigest() in PRIVATE_NAME_HASHES:
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


def test_no_public_page_names_a_private_person_host_or_agent() -> None:
    found = []
    for path in _public_markdown():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if _names_in(line):
                # The line number only: printing the name would publish it in CI logs.
                found.append(f"{path.relative_to(REPO_ROOT)}:{number}")
    assert not found, "private names in public pages (see the hashes in this test):\n" + "\n".join(found)


def test_the_name_check_finds_a_listed_name_inside_an_alias_and_a_path(monkeypatch) -> None:
    # A made-up name stands in for a listed one, so this test names nobody.
    probe = "zq-probe"
    monkeypatch.setattr(
        __import__(__name__), "PRIVATE_NAME_HASHES",
        PRIVATE_NAME_HASHES | {hashlib.sha256(probe.encode()).hexdigest()},
    )
    assert _names_in("agent-zq-probe@host-two") == [probe]
    assert _names_in("~/.kdcube/pb/workspaces/ZQ-Probe/applications") == [probe]
    assert _names_in("zq-prober and probe-zq") == []
    # The placeholders the public pages use are not listed names.
    assert _names_in("agent-one@host-two, my-agent, maintainer-host, agent-user") == []
    # The repository's own address is not a private name.
    assert _names_in("git clone https://github.com/zq-probe/app-ecosystem.git") == []
    assert len(PRIVATE_NAME_HASHES) >= 20
