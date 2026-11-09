"""Wait until the package's own install resolves its first-party floors (W1016).

The publish workflow publishes the pb set one package after another. A package
that requires another of the set at this exact version must not start its
tests before pip can install that version. Publish run 42207266620 (6 Nov
2026) showed that "the file is on the index" is not that proof:
`pip download --no-deps connection-hub==2026.11.6.1119` succeeded at 11:06:46Z
and `pip install -e .[test]` failed four seconds later with "No matching
distribution found for connection-hub<2027,>=2026.11.06.1119", because PyPI's
index pages had not updated on every edge yet.

So the wait runs the install the next step runs, as a dry run, and accepts
only several consecutive successes: one success can come from an edge that is
already current while the next request reaches one that is not.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
import tomllib
from collections.abc import Callable
from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import Version

FIRST_PARTY = {
    "app-foundation", "service-foundation", "harness-foundation",
    "capabilities-foundation", "economics-foundation",
    "connection-hub", "connection-hub-cli", "project-board",
}


def waiting_for(pyproject: Path, version: str) -> list[str]:
    """The first-party packages this one requires at exactly `version` or later."""

    expected = Version(version)
    project = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]
    declared = list(project.get("dependencies", []))
    for extra in project.get("optional-dependencies", {}).values():
        declared.extend(extra)
    names = set()
    for value in declared:
        requirement = Requirement(value)
        if requirement.name in FIRST_PARTY and any(
            spec.operator == ">=" and Version(spec.version) == expected for spec in requirement.specifier
        ):
            names.add(requirement.name)
    return sorted(names)


def install_target(package_path: str, tested: bool) -> str:
    """What the workflow's "Test package" step installs, editable."""

    return f"{package_path}[test]" if tested else package_path


PROBE_LIMIT = 5 * 60


def resolves(target: str, timeout: float) -> bool:
    """Whether `pip install -e <target>` would succeed now, without installing it.

    A resolver or build backend that does not finish within `timeout` seconds
    is a failed check, never a success.
    """

    try:
        return subprocess.run(
            [sys.executable, "-m", "pip", "install", "--dry-run", "--ignore-installed",
             "--no-cache-dir", "--quiet", "-e", target],
            capture_output=True,
            timeout=timeout,
        ).returncode == 0
    except subprocess.TimeoutExpired:
        return False


def wait(
    target: str,
    names: list[str],
    *,
    probe: Callable[[str, float], bool] = resolves,
    needed: int = 3,
    interval: float = 20.0,
    limit: float = 20 * 60,
    probe_limit: float = PROBE_LIMIT,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    say: Callable[[str], None] = print,
) -> int:
    """Return the number of probes once `needed` consecutive ones resolved.

    Everything happens inside `limit` seconds: each probe gets at most the
    budget that remains (and at most `probe_limit`), a success that ends after
    the deadline does not count, and no sleep runs past it. Raises SystemExit
    when the install has not resolved `needed` times in a row in time.
    """

    if not names:
        say("no first-party requirement at this version; nothing to wait for")
        return 0
    deadline = clock() + limit
    streak = probes = 0
    while True:
        remaining = deadline - clock()
        if remaining <= 0:
            raise SystemExit(
                f"the install of {target} did not resolve {', '.join(names)} "
                f"{needed} times in a row within {int(limit // 60)} minutes"
            )
        probes += 1
        resolved = probe(target, min(remaining, probe_limit))
        if clock() > deadline:
            continue  # a check that ended after the deadline proves nothing; the top of the loop fails
        if resolved:
            streak += 1
            if streak >= needed:
                say(f"the install of {target} resolves {', '.join(names)} ({streak} consecutive checks)")
                return probes
        else:
            streak = 0
        sleep(max(0.0, min(interval, deadline - clock())))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--package-path", required=True)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--tested", action="store_true", help="the next step installs the [test] extra")
    args = parser.parse_args(argv)
    names = waiting_for(Path(args.package_path) / "pyproject.toml", args.expected_version)
    wait(install_target(args.package_path, args.tested), names)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
