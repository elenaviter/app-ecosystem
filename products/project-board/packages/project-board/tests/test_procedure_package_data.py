"""Every file the worker procedure declares ships in the installed package (W563).

Why: the 03:35 UTC client window on 6 October 2026 was cancelled before any
switch. Procedure .14 split coordinator.md and collaboration.md into modules
under references/coordinator/ and references/collaboration/, and the package
data rule matched only references/*.md, so an installed copy lacked every
module and `pb procedure install` refused with work_agent_procedure_file_missing.
The other tests read the source tree, which has the files.
"""

from __future__ import annotations

import fnmatch
import json
import tomllib
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SOURCE = PACKAGE_ROOT / "src" / "project_board"
PROCEDURE = SOURCE / "procedures" / "problem-board-worker"


def _package_data() -> list[str]:
    config = tomllib.loads((PACKAGE_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return list(config["tool"]["setuptools"]["package-data"]["project_board"])


def _shipped(relative: str, patterns: list[str]) -> bool:
    # setuptools globs: "*" does not cross a path separator.
    parts = relative.split("/")
    return any(
        len(pattern.split("/")) == len(parts)
        and all(fnmatch.fnmatchcase(part, glob) for part, glob in zip(parts, pattern.split("/")))
        for pattern in patterns
    )


def test_every_declared_procedure_file_matches_a_package_data_rule() -> None:
    manifest = json.loads((PROCEDURE / "package.json").read_text(encoding="utf-8"))
    declared = [manifest["entrypoint"], *manifest["references"], "package.json"]
    patterns = _package_data()
    missing = [
        path for path in declared
        if not _shipped(f"procedures/problem-board-worker/{path}", patterns)
    ]
    assert missing == [], f"declared but not packaged: {missing}"


def test_every_procedure_markdown_file_in_the_tree_is_packaged() -> None:
    patterns = _package_data()
    missing = [
        str(path.relative_to(SOURCE))
        for path in sorted((SOURCE / "procedures").rglob("*.md"))
        if not _shipped(str(path.relative_to(SOURCE)).replace("\\", "/"), patterns)
    ]
    assert missing == [], f"procedure files not packaged: {missing}"
