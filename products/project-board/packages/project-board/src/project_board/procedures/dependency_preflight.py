"""Name the first declared third-party dependency an interpreter lacks.

Usage: <interpreter> procedures/dependency_preflight.py <pyproject.toml>...

Reads ``[project].dependencies`` of each pyproject given, skips a requirement
whose ``python_version`` marker excludes this interpreter and one that names
another of the given projects (a source overlay, on ``PYTHONPATH`` and not
installed), and asks ``importlib.metadata`` for each remaining distribution
in the interpreter that runs this script. Every line it prints names that
interpreter, because the answer differs per host. Exit 1 on the first missing
distribution, naming the requirement and the pyproject that declares it.

What it does not check: a dependency that is imported but undeclared, a
version that is present but outside the declared range, and extras
(``[project.optional-dependencies]``). It settles one question: can this
interpreter see every distribution these projects declare.

Why: a suite run from an interpreter that lacks a declared dependency passes
until a change imports it at module level, and then many modules fail to
collect with errors that read nothing like the cause (2026-09-23, jwcrypto,
readchar, json5 in one venv).
"""

from __future__ import annotations

import importlib.metadata as metadata
import re
import sys
import tomllib
from pathlib import Path

_COMPARE = {
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
}


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def distribution_name(requirement: str) -> str:
    return re.split(r"[\[<>=!~;\s]", requirement.strip(), 1)[0]


def marker_excludes(requirement: str, version: tuple[int, int]) -> bool:
    """True when a ``python_version`` marker rules this interpreter out."""
    if ";" not in requirement:
        return False
    marker = requirement.split(";", 1)[1]
    match = re.search(r"python_version\s*(<=|>=|==|!=|<|>)\s*['\"]([\d.]+)['\"]", marker)
    if match is None:
        return False
    wanted = tuple(int(part) for part in match.group(2).split(".")[:2])
    return not _COMPARE[match.group(1)](version, wanted)


def load_project(path: Path) -> tuple[str | None, list[str]]:
    with path.open("rb") as handle:
        project = tomllib.load(handle).get("project", {})
    return project.get("name"), list(project.get("dependencies") or [])


def run(paths: list[Path], interpreter: str, version: tuple[int, int]) -> tuple[int, list[str]]:
    projects = [(path, *load_project(path)) for path in paths]
    overlays = {normalize(name) for _, name, _ in projects if name}
    checked = 0
    missing: list[tuple[str, str, Path]] = []
    for path, _, requirements in projects:
        for requirement in requirements:
            name = distribution_name(requirement)
            if normalize(name) in overlays or marker_excludes(requirement, version):
                continue
            checked += 1
            try:
                metadata.version(name)
            except metadata.PackageNotFoundError:
                missing.append((name, requirement, path))
    if not missing:
        return 0, [f"preflight ok: {checked} declared distributions present in {interpreter}"]
    lines = []
    for index, (name, requirement, path) in enumerate(missing):
        lead = "missing" if index == 0 else "also missing"
        lines.append(f"{lead}: {name} (declared as '{requirement}' in {path}) in {interpreter}")
    lines.append(f"{len(missing)} missing of {checked} declared in {interpreter}")
    return 1, lines


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__.strip().splitlines()[0])
        print("usage: <interpreter> dependency_preflight.py <pyproject.toml>...")
        return 2
    code, lines = run([Path(arg) for arg in argv], sys.executable, sys.version_info[:2])
    print("\n".join(lines))
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
