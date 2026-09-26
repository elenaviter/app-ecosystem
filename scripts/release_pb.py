#!/usr/bin/env python3
"""Release the pb set: app-foundation, service-foundation, connection-hub, project-board.

One version for the whole set (W322), cut, gated, tagged, published and
verified by one command in three steps (W364):

    scripts/release-pb prepare <YYYY.MM.DD.HHMM> --notes <file> [--dry-run]
    scripts/release-pb publish <YYYY.MM.DD.HHMM> [--dry-run]
    scripts/release-pb verify  <YYYY.MM.DD.HHMM>

`prepare` runs from a clean checkout of origin/main. It sets the version in
every file of the set, writes the release notes into each release record,
runs each package's gate the way the publish workflow does (a fresh virtual
environment, `pip install -e "<path>[test]"`, no source overlay; the set's
own packages come from the wheels just built, since the index does not have
them yet), builds and checks every distribution, smoke-installs every wheel,
then commits the release on `release/<version>` and opens its pull request.
`--dry-run` does all of that in a throwaway worktree and stops before the
commit.

`publish` runs after that pull request is merged. It tags the release merge
commit, dispatches `publish-python-package.yml` with `package=pb-set` (the
workflow publishes the four packages one after another and stops at the first
failure), waits for the run, then runs `verify`.

`verify` checks each version on PyPI, installs `project-board==<version>` in a
clean environment, checks `pb --version`, and prints the line hosts run.
"""

from __future__ import annotations

import argparse
import calendar
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]
WORKFLOW = "publish-python-package.yml"
SET_INPUT = "pb-set"
VERSION_RE = re.compile(r"^\d{4}\.\d{2}\.\d{2}\.\d{4}$")
INDEX_WAIT_SECONDS = 20 * 60


@dataclass(frozen=True)
class Package:
    name: str
    path: str
    import_name: str
    # The publish workflow runs the package's tests for these; the others get
    # an install, import and version smoke.
    tested: bool


# Dependency order: the workflow publishes in this order, and a package's
# gate installs the ones before it from the wheels built here.
SET = (
    Package("app-foundation", "packages/app-foundation", "app_foundation", False),
    Package("service-foundation", "packages/service-foundation", "service_foundation", False),
    Package("connection-hub", "products/connection-hub/packages/connection-hub", "connection_hub", True),
    Package("project-board", "products/project-board/packages/project-board", "project_board", True),
)

# Where the set's version lives. connection-hub-cli moves with the set (the
# product release record names it), and is published on its own when it is
# releasable.
VERSION_ROOTS = (
    "packages/app-foundation",
    "packages/service-foundation",
    "products/connection-hub/release.yaml",
    "products/connection-hub/packages/connection-hub",
    "products/connection-hub/packages/connection-hub-cli",
    "products/project-board/packages/project-board",
)

# Each record's `description: |` block becomes the release notes.
RELEASE_RECORDS = (
    "packages/app-foundation/release.yaml",
    "packages/service-foundation/release.yaml",
    "products/connection-hub/release.yaml",
    "products/project-board/packages/project-board/release.yaml",
)


class ReleaseError(RuntimeError):
    pass


def say(message: str) -> None:
    print(f"release-pb: {message}", flush=True)


def run(args: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None,
        capture: bool = False, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        [str(arg) for arg in args],
        cwd=str(cwd) if cwd else None,
        env=env,
        text=True,
        capture_output=capture,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip() if capture else ""
        raise ReleaseError(f"command failed ({result.returncode}): {' '.join(map(str, args))}\n{detail}".rstrip())
    return result


def git(*args: str, cwd: Path = REPOSITORY, check: bool = True) -> str:
    return run(["git", *args], cwd=cwd, capture=True, check=check).stdout.strip()


def clean_env() -> dict[str, str]:
    """The environment without a source overlay: the 2026-09-26 lesson."""

    env = dict(os.environ)
    for name in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "PIP_REQUIRE_VIRTUALENV"):
        env.pop(name, None)
    return env


def version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def canonical(version: str) -> str:
    """PEP 440's normal form, as PyPI and `pb --version` print it."""

    return ".".join(str(part) for part in version_tuple(version))


def read_version(root: Path) -> str:
    text = (root / "products/project-board/packages/project-board/pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version = "([^"]+)"$', text, flags=re.M)
    if not match:
        raise ReleaseError("project-board's pyproject.toml has no version")
    return match.group(1)


def check_new_version(current: str, new: str) -> None:
    if not VERSION_RE.match(new):
        raise ReleaseError(f"version must be YYYY.MM.DD.HHMM (Berlin time), got {new!r}")
    if version_tuple(new) <= version_tuple(current):
        raise ReleaseError(f"version {new} is not newer than the current set version {current}")


def version_files(root: Path, version: str) -> list[Path]:
    """Every tracked file of the set that names `version`."""

    listed = run(["git", "ls-files", "-z", "--", *VERSION_ROOTS], cwd=root, capture=True).stdout
    files = []
    for name in filter(None, listed.split("\0")):
        path = root / name
        try:
            if version in path.read_text(encoding="utf-8"):
                files.append(path)
        except (UnicodeDecodeError, FileNotFoundError):
            continue
    return files


def replace_description(text: str, notes: str) -> str:
    """Replace the `  description: |` block of a release record with `notes`."""

    lines = text.split("\n")
    for index, line in enumerate(lines):
        if re.match(r"^  description: \|-?$", line):
            end = index + 1
            while end < len(lines) and (lines[end].strip() == "" or lines[end].startswith("    ")):
                end += 1
            # Keep the blank line that separates the block from the next key.
            while end > index + 1 and lines[end - 1].strip() == "":
                end -= 1
            body = [("    " + row) if row.strip() else "" for row in notes.strip("\n").split("\n")]
            return "\n".join(lines[: index + 1] + body + lines[end:])
    raise ReleaseError("release record has no `  description: |` block")


def apply_version(root: Path, current: str, new: str, notes: str) -> list[Path]:
    """Set `new` everywhere the set names `current`, and the notes in each record."""

    changed = version_files(root, current)
    if not changed:
        raise ReleaseError(f"no file of the set names the current version {current}")
    for path in changed:
        path.write_text(path.read_text(encoding="utf-8").replace(current, new), encoding="utf-8")
    for record in RELEASE_RECORDS:
        path = root / record
        path.write_text(replace_description(path.read_text(encoding="utf-8"), notes), encoding="utf-8")
        if path not in changed:
            changed.append(path)
    left = version_files(root, current)
    if left:
        names = ", ".join(str(path.relative_to(root)) for path in left)
        raise ReleaseError(f"{current} is still named in: {names}")
    for package in SET:
        pyproject = (root / package.path / "pyproject.toml").read_text(encoding="utf-8")
        if f'version = "{new}"' not in pyproject:
            raise ReleaseError(f"{package.name} does not carry {new}")
    return sorted(changed)


def pick_python(requested: str | None) -> str:
    if requested:
        return requested
    for candidate in ("python3.11", "python3"):
        found = shutil.which(candidate)
        if found:
            return found
    return sys.executable


def venv(python: str, where: Path) -> Path:
    run([python, "-m", "venv", where], env=clean_env())
    bin_dir = where / ("Scripts" if os.name == "nt" else "bin")
    run([bin_dir / "python", "-m", "pip", "install", "--quiet", "--upgrade", "pip"], env=clean_env())
    return bin_dir


def check_import(bin_dir: Path, package: Package, version: str) -> None:
    code = (
        "import importlib, sys\n"
        "name, expected = sys.argv[1], sys.argv[2]\n"
        "actual = getattr(importlib.import_module(name), '__version__', '')\n"
        "if actual != expected:\n"
        "    raise SystemExit(f'{name} reports {actual!r}, expected {expected!r}')\n"
    )
    run([bin_dir / "python", "-c", code, package.import_name, version], env=clean_env())


def gate(root: Path, version: str, python: str, scratch: Path) -> list[str]:
    """Each package's publish-workflow gate, locally, in dependency order."""

    report = []
    dist = scratch / "dist"
    tools = venv(python, scratch / "tools")
    run([tools / "python", "-m", "pip", "install", "--quiet", "build", "twine", "readme_renderer[md]"], env=clean_env())
    for package in SET:
        out = dist / package.name
        say(f"build {package.name}")
        run([tools / "python", "-m", "build", "--outdir", out, root / package.path], env=clean_env(), capture=True)
        run([tools / "python", "-m", "twine", "check", "--strict", *sorted(out.iterdir())], env=clean_env(), capture=True)
    find_links = [arg for package in SET for arg in ("--find-links", dist / package.name)]
    for package in SET:
        say(f"gate {package.name}: fresh environment, no source overlay")
        bin_dir = venv(python, scratch / f"test-{package.name}")
        target = f"{root / package.path}[test]" if package.tested else str(root / package.path)
        run([bin_dir / "python", "-m", "pip", "install", "--quiet", *find_links, "-e", target], env=clean_env())
        check_import(bin_dir, package, version)
        if package.tested:
            run([bin_dir / "python", "-m", "pytest", "-q", "-p", "no:cacheprovider", root / package.path / "tests"],
                cwd=root, env=clean_env())
        smoke = venv(python, scratch / f"smoke-{package.name}")
        wheels = sorted((dist / package.name).glob("*.whl"))
        run([smoke / "python", "-m", "pip", "install", "--quiet", *find_links, *wheels], env=clean_env())
        check_import(smoke, package, version)
        if package.name == "project-board":
            printed = run([smoke / "pb", "--version"], env=clean_env(), capture=True).stdout.strip()
            if printed != f"problem-board {canonical(version)}":
                raise ReleaseError(f"pb --version printed {printed!r}")
        report.append(f"{package.name}: {'tests passed, ' if package.tested else ''}build, twine check and wheel smoke passed")
    return report


def require_clean_main() -> None:
    if git("status", "--porcelain"):
        raise ReleaseError("the working tree is not clean; run from a clean checkout of origin/main")
    git("fetch", "--quiet", "--tags", "origin")
    if git("rev-parse", "HEAD") != git("rev-parse", "origin/main"):
        raise ReleaseError("HEAD is not origin/main; check out origin/main first")


def tag_exists(version: str) -> bool:
    return bool(git("ls-remote", "--tags", "origin", f"refs/tags/{version}"))


def prepare(args: argparse.Namespace) -> int:
    notes_path = Path(args.notes)
    notes = notes_path.read_text(encoding="utf-8").strip()
    if not notes:
        raise ReleaseError(f"{notes_path} is empty; the release records need the changes")
    if not args.dry_run:
        require_clean_main()
    current = read_version(REPOSITORY)
    check_new_version(current, args.version)
    if tag_exists(args.version):
        raise ReleaseError(f"tag {args.version} already exists on origin")
    python = pick_python(args.python)
    scratch = Path(tempfile.mkdtemp(prefix=f"release-pb-{args.version}-"))
    worktree = scratch / "tree"
    try:
        if args.dry_run:
            git("worktree", "add", "--quiet", "--detach", str(worktree), "HEAD")
            root = worktree
        else:
            branch = f"release/{args.version}"
            git("switch", "--quiet", "-c", branch)
            root = REPOSITORY
        changed = apply_version(root, current, args.version, notes)
        say(f"{current} -> {args.version} in {len(changed)} files")
        report = [] if args.skip_gate else gate(root, args.version, python, scratch)
        for line in report:
            say(line)
        if args.dry_run:
            print(git("diff", "--stat", cwd=root))
            say("dry run: nothing committed, pushed or opened")
            return 0
        git("add", "--", *[str(path.relative_to(REPOSITORY)) for path in changed])
        message = f"Release the pb set {args.version}\n\n{notes}\n"
        subprocess.run(["git", "commit", "--quiet", "-F", "-"], cwd=REPOSITORY, input=message, text=True, check=True)
        git("push", "--quiet", "-u", "origin", f"release/{args.version}")
        body = "\n".join([
            f"Releases app-foundation, service-foundation, connection-hub and project-board {args.version} (connection-hub-cli carries the version).",
            "",
            notes,
            "",
            "Gate, run by `scripts/release-pb prepare` in fresh environments without a source overlay:",
            *[f"- {line}" for line in report],
            "",
            f"After merge: `scripts/release-pb publish {args.version}`.",
        ])
        url = run(["gh", "pr", "create", "--base", "main", "--head", f"release/{args.version}",
                   "--title", f"Release the pb set {args.version}", "--body", body], capture=True).stdout.strip()
        say(f"release pull request: {url}")
        say(f"after it is merged: scripts/release-pb publish {args.version}")
        return 0
    finally:
        if args.dry_run and worktree.exists():
            git("worktree", "remove", "--force", str(worktree), check=False)
        shutil.rmtree(scratch, ignore_errors=True)


def release_commit(version: str, *, cwd: Path = REPOSITORY, ref: str = "origin/main") -> str:
    """The first-parent commit of `ref` where the set became `version`.

    Later commits on main keep the version until the next release; the tag
    goes on the merge that brought it in, not on whatever main is now.
    """

    path = "products/project-board/packages/project-board/pyproject.toml"
    found = ""
    for commit in git("rev-list", "--first-parent", ref, cwd=cwd).split():
        text = git("show", f"{commit}:{path}", cwd=cwd, check=False)
        if f'version = "{version}"' in text:
            found = commit
            continue
        break
    if not found:
        raise ReleaseError(f"{ref} does not carry {version}; merge the release pull request first")
    return found


def dispatch_and_wait(version: str) -> None:
    started = time.time()
    run(["gh", "workflow", "run", WORKFLOW, "--ref", version,
         "-f", f"package={SET_INPUT}", "-f", f"expected_version={version}"])
    run_id = ""
    for _ in range(30):
        time.sleep(5)
        listed = json.loads(run(["gh", "run", "list", "--workflow", WORKFLOW, "--limit", "10",
                                 "--json", "databaseId,headBranch,event,createdAt"], capture=True).stdout)
        for row in listed:
            created = calendar.timegm(time.strptime(row["createdAt"], "%Y-%m-%dT%H:%M:%SZ"))
            if row["headBranch"] == version and row["event"] == "workflow_dispatch" and created >= started - 60:
                run_id = str(row["databaseId"])
                break
        if run_id:
            break
    if not run_id:
        raise ReleaseError("the publish run did not appear; check the Actions tab")
    say(f"publish run {run_id}: waiting")
    result = run(["gh", "run", "watch", run_id, "--exit-status", "--interval", "30"], check=False)
    if result.returncode != 0:
        raise ReleaseError(f"publish run {run_id} failed; the packages after the failed one were not published")


def publish(args: argparse.Namespace) -> int:
    git("fetch", "--quiet", "--tags", "origin")
    if tag_exists(args.version):
        raise ReleaseError(f"tag {args.version} already exists on origin; run verify, or cut a new version")
    commit = release_commit(args.version)
    say(f"release commit {commit[:12]} on origin/main")
    if args.dry_run:
        say(f"dry run: would tag {args.version} at {commit[:12]}, dispatch {WORKFLOW} package={SET_INPUT}, wait, then verify")
        return 0
    git("tag", "-a", args.version, "-m", f"pb set {args.version}", commit)
    git("push", "--quiet", "origin", f"refs/tags/{args.version}")
    dispatch_and_wait(args.version)
    return verify(args)


def on_pypi(name: str, version: str) -> bool:
    try:
        with urllib.request.urlopen(f"https://pypi.org/pypi/{name}/{canonical(version)}/json", timeout=30) as reply:
            return json.load(reply)["info"]["version"] == canonical(version)
    except (urllib.error.URLError, KeyError, ValueError):
        return False


def verify(args: argparse.Namespace) -> int:
    version = args.version
    missing = [package.name for package in SET if not on_pypi(package.name, version)]
    if missing:
        raise ReleaseError(f"not on PyPI at {canonical(version)}: {', '.join(missing)}")
    say(f"PyPI has all four at {canonical(version)}")
    python = pick_python(getattr(args, "python", None))
    scratch = Path(tempfile.mkdtemp(prefix=f"release-pb-verify-{version}-"))
    try:
        bin_dir = venv(python, scratch / "venv")
        deadline = time.monotonic() + INDEX_WAIT_SECONDS
        while True:
            installed = run([bin_dir / "python", "-m", "pip", "install", "--quiet", "--no-cache-dir",
                             f"project-board=={version}"], env=clean_env(), capture=True, check=False)
            if installed.returncode == 0:
                break
            if time.monotonic() > deadline:
                raise ReleaseError(f"project-board=={version} did not install from the index:\n{installed.stderr}")
            say("the index does not serve it yet; retrying in 20 s")
            time.sleep(20)
        printed = run([bin_dir / "pb", "--version"], env=clean_env(), capture=True).stdout.strip()
        if printed != f"problem-board {canonical(version)}":
            raise ReleaseError(f"pb --version printed {printed!r}")
        frozen = run([bin_dir / "python", "-m", "pip", "freeze"], env=clean_env(), capture=True).stdout
        for package in SET:
            if f"{package.name}=={canonical(version)}" not in frozen:
                raise ReleaseError(f"a clean install resolved another {package.name}:\n{frozen}")
        say(f"a clean install of project-board=={version} resolves the set and prints {printed!r}")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    print(f"\nHosts move to it with:\n\n  pb source use-release --expect-version {version}\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts/release-pb", description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    step = commands.add_parser("prepare", help="set the version, run the gate, open the release pull request")
    step.add_argument("version")
    step.add_argument("--notes", required=True, help="file with the changes, written into each release record")
    step.add_argument("--dry-run", action="store_true", help="do everything in a throwaway worktree; commit nothing")
    step.add_argument("--skip-gate", action="store_true", help=argparse.SUPPRESS)
    step.add_argument("--python", help="interpreter for the gate environments (default: python3.11)")
    step.set_defaults(handler=prepare)
    step = commands.add_parser("publish", help="after the release pull request is merged: tag, publish, verify")
    step.add_argument("version")
    step.add_argument("--dry-run", action="store_true", help="find the release commit and say what would happen")
    step.add_argument("--python", help="interpreter for the verification environment")
    step.set_defaults(handler=publish)
    step = commands.add_parser("verify", help="check PyPI and a clean install of project-board==<version>")
    step.add_argument("version")
    step.add_argument("--python", help="interpreter for the verification environment")
    step.set_defaults(handler=verify)
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except ReleaseError as exc:
        print(f"release-pb: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
