#!/usr/bin/env python3
"""Release the pb set: app-foundation, service-foundation, connection-hub, project-board.

One version for the whole set (W322), cut, gated, tagged, published and
verified by one command in three steps (W364):

    scripts/release-pb prepare <YYYY.MM.DD.HHMM> --notes <file> --scratch <run> [--dry-run] [route]
    scripts/release-pb publish <YYYY.MM.DD.HHMM> --scratch <run> [--dry-run] [route]
    scripts/release-pb verify  <YYYY.MM.DD.HHMM> --scratch <run>

    route: --project-ref <project> --github-login <login>
           [--runtime-kind <kind> --runtime-session-id <id>]

`prepare` runs in the release's own worktree, made from origin/main
(`git -C <clone> worktree add <workspace>/wt/release-<version> origin/main`),
never in a clone's main checkout that other work reads. It sets the version in
every file of the set, writes the release notes into each release record,
runs each package's gate the way the publish workflow does (a fresh virtual
environment, `pip install -e "<path>[test]"`, no source overlay; the set's
own packages come from the wheels just built, since the index does not have
them yet), builds and checks every distribution, smoke-installs every wheel,
then commits the release on `release/<version>` and opens its pull request.
`--dry-run` does all of that in the same tree and stops before the commit,
then puts back the files it changed.

`publish` runs after that pull request is merged. It tags the release merge
commit, dispatches `publish-python-package.yml` with `package=pb-set` (the
workflow publishes the four packages one after another and stops at the first
failure), waits for the run, then runs `verify`.

`verify` checks each version on PyPI, installs `project-board==<version>` in a
clean environment, checks `pb --version`, and prints the line hosts run.

Every GitHub operation goes through the agent's governed route, `pb worker gh`
and `pb worker push` for one named project (W454): never an ambient `gh`
login, and never the deploy key. Environments and built distributions go in
the `--scratch` folder the caller names (a `pb worker scratch --new` run), and
the script removes only the subfolder it made there.
"""

from __future__ import annotations

import argparse
import calendar
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
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
# The remote `pb worker connect-project` adds for this machine's deploy key.
DEPLOY_KEY_REMOTE = "deploykey"


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

# The managed procedure package is versioned by its own revision ledger, not
# by the set: a version named there is the release a behaviour came with, and
# rewriting it would change the package's digest under its recorded revision,
# so every host's `pb procedure verify` would fail on the release (W454).
VERSION_EXCLUDED = ("products/project-board/packages/project-board/src/project_board/procedures/",)

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


@dataclass(frozen=True)
class GitHubRoute:
    """GitHub through the agent's governed route for one project (W454).

    `pb worker gh` runs gh with the project owner's GitHub key for one command,
    and `pb worker push` pushes with it. A push fails closed before it writes:
    the remote must be HTTPS, which only the owner key's credential helper
    answers (an SSH remote would push with whatever key the machine has), and
    the clone must not have the `deploykey` remote, because `pb worker push`
    retries through that remote when the owner key is unavailable. Then the
    route asks GitHub who it answers as and stops unless that is `login`. A
    push that still reports the deploy key stops the release.
    """

    project_ref: str
    login: str
    runtime: tuple[str, ...] = ()
    pb: str = "pb"

    def command(self, verb: str, args: list[str]) -> list[str]:
        return [self.pb, "worker", verb, "--project-ref", self.project_ref, *self.runtime, "--", *args]

    def gh(self, args: list[str], *, cwd: Path, capture: bool = True, check: bool = True) -> subprocess.CompletedProcess:
        return run(self.command("gh", args), cwd=cwd, capture=capture, check=check)

    def check_actor(self, *, cwd: Path) -> None:
        answer = self.gh(["api", "user", "--jq", ".login"], cwd=cwd, check=False)
        if answer.returncode != 0:
            raise ReleaseError(f"the owner's GitHub key did not answer; nothing was pushed:\n{answer.stderr.strip()}")
        if answer.stdout.strip() != self.login:
            raise ReleaseError(f"GitHub answers as {answer.stdout.strip()!r}, not {self.login!r}; nothing was pushed")

    def check_push_target(self, remote: str, *, cwd: Path) -> None:
        remotes = git("remote", cwd=cwd).split()
        if DEPLOY_KEY_REMOTE in remotes:
            raise ReleaseError(
                f"this clone has the {DEPLOY_KEY_REMOTE!r} remote, which `pb worker push` falls back to "
                "when the owner key is unavailable; a release pushes only with the owner key, so it does not push from here"
            )
        url = git("remote", "get-url", "--push", remote, cwd=cwd)
        if not url.startswith("https://"):
            raise ReleaseError(f"remote {remote!r} pushes to {url!r}; a release pushes over HTTPS with the owner key only")

    def push(self, remote: str, refs: list[str], *, cwd: Path) -> None:
        self.check_push_target(remote, cwd=cwd)
        self.check_actor(cwd=cwd)
        args = ["--quiet", remote, *refs]
        result = run(self.command("push", args), cwd=cwd, capture=True, check=False)
        if "deploy key" in result.stderr:
            raise ReleaseError(
                "the push reported the deploy-key fallback, which a release may not use; "
                f"stop and report it, do not retry through another route:\n{result.stderr.strip()}"
            )
        if result.returncode != 0:
            raise ReleaseError(f"push failed ({result.returncode}): {' '.join(args)}\n{result.stderr.strip()}")


def github_route(args: argparse.Namespace) -> GitHubRoute:
    if not args.project_ref or not args.github_login:
        raise ReleaseError("name the governed GitHub route: --project-ref <project> --github-login <owner's login>")
    runtime: tuple[str, ...] = ()
    if args.runtime_kind:
        runtime += ("--runtime-kind", args.runtime_kind)
    if args.runtime_session_id:
        runtime += ("--runtime-session-id", args.runtime_session_id)
    return GitHubRoute(args.project_ref, args.github_login, runtime)


def require_own_worktree(root: Path = REPOSITORY) -> None:
    """The release changes files and switches branches: only in its own linked worktree."""

    git_dir = (root / git("rev-parse", "--git-dir", cwd=root)).resolve()
    common = (root / git("rev-parse", "--git-common-dir", cwd=root)).resolve()
    if git_dir == common:
        raise ReleaseError(
            f"{root} is a clone's main checkout; run from the release's own worktree: "
            "git -C <clone> worktree add <workspace>/wt/release-<version> origin/main"
        )


def ignored_paths(root: Path) -> set[str]:
    listed = git("status", "--porcelain", "-z", "--ignored", cwd=root)
    return {entry[3:] for entry in listed.split("\0") if entry.startswith("!! ")}


def remove_new_ignored(root: Path, before: set[str]) -> list[str]:
    """Remove the ignored build outputs the run made in the set's packages, nothing else."""

    removed = []
    for name in sorted(ignored_paths(root) - before):
        if not any(name.startswith(prefix.rstrip("/") + "/") for prefix in VERSION_ROOTS):
            continue
        path = root / name
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        elif path.exists() or path.is_symlink():
            path.unlink()
        removed.append(name)
    return removed


def scratch_folder(args: argparse.Namespace, step: str) -> Path:
    """The run's own subfolder of the caller's scratch folder; never one that exists."""

    base = Path(args.scratch).expanduser().resolve()
    if not base.is_dir():
        raise ReleaseError(f"--scratch {base} is not a folder; make one with `pb worker scratch --new`")
    folder = base / f"release-pb-{step}-{args.version}"
    if folder.exists():
        raise ReleaseError(f"{folder} exists from an earlier run; read it, then remove it or name another --scratch")
    folder.mkdir()
    return folder


def drop_scratch(folder: Path, keep: bool) -> None:
    if keep:
        say(f"kept {folder}")
    else:
        shutil.rmtree(folder, ignore_errors=True)


def clean_env() -> dict[str, str]:
    """The environment without a source overlay: the 2026-09-26 lesson.

    A release also requires the procedure package's content to be recorded
    under its revision: without this the revision tests skip instead of fail.
    """

    env = dict(os.environ)
    for name in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "PIP_REQUIRE_VIRTUALENV"):
        env.pop(name, None)
    env["PB_REQUIRE_REVISION_RECORDED"] = "1"
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
        if name.startswith(VERSION_EXCLUDED):
            continue
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
    # The distributions go with the scratch folder; their hashes stay in the report.
    for package in SET:
        for artifact in sorted((dist / package.name).iterdir()):
            report.append(f"sha256 {hashlib.sha256(artifact.read_bytes()).hexdigest()}  {artifact.name}")
    return report


def require_clean(root: Path = REPOSITORY) -> None:
    if git("status", "--porcelain", cwd=root):
        raise ReleaseError("the working tree is not clean; the release runs in a clean tree of its own")


def require_clean_main(root: Path = REPOSITORY) -> None:
    require_clean(root)
    git("fetch", "--quiet", "--tags", "origin", cwd=root)
    if git("rev-parse", "HEAD", cwd=root) != git("rev-parse", "origin/main", cwd=root):
        raise ReleaseError("HEAD is not origin/main; make the release tree from origin/main")


def tag_exists(version: str) -> bool:
    return bool(git("ls-remote", "--tags", "origin", f"refs/tags/{version}"))


def open_release_pull_request(github: GitHubRoute, version: str, body: str, *, root: Path = REPOSITORY) -> str:
    """Open the release pull request and check GitHub holds the commit that was gated."""

    branch = f"release/{version}"
    url = github.gh(["pr", "create", "--base", "main", "--head", branch,
                     "--title", f"Release the pb set {version}", "--body", body], cwd=root).stdout.strip()
    shown = json.loads(github.gh(["pr", "view", url, "--json", "headRefName,baseRefName,headRefOid"], cwd=root).stdout)
    expected = {"headRefName": branch, "baseRefName": "main", "headRefOid": git("rev-parse", "HEAD", cwd=root)}
    if {key: shown.get(key) for key in expected} != expected:
        raise ReleaseError(f"{url} is not the gated release: GitHub shows {shown}, expected {expected}")
    return url


def prepare(args: argparse.Namespace) -> int:
    notes_path = Path(args.notes)
    notes = notes_path.read_text(encoding="utf-8").strip()
    if not notes:
        raise ReleaseError(f"{notes_path} is empty; the release records need the changes")
    github = None if args.dry_run else github_route(args)
    require_own_worktree()
    if args.dry_run:
        require_clean()
    else:
        require_clean_main()
    current = read_version(REPOSITORY)
    check_new_version(current, args.version)
    if tag_exists(args.version):
        raise ReleaseError(f"tag {args.version} already exists on origin")
    python = pick_python(args.python)
    root = REPOSITORY
    built_before = ignored_paths(root)
    scratch = scratch_folder(args, "prepare")
    try:
        if not args.dry_run:
            git("switch", "--quiet", "-c", f"release/{args.version}")
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
        github.push("origin", [f"release/{args.version}"], cwd=root)
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
        url = open_release_pull_request(github, args.version, body, root=root)
        say(f"release pull request: {url}")
        say(f"after it is merged: scripts/release-pb publish {args.version}")
        return 0
    finally:
        if args.dry_run:
            # The tree was clean when the run started, so every change in it is this run's.
            written = [name for name in git("diff", "--name-only", "-z", cwd=root).split("\0") if name]
            if written:
                git("restore", "--source=HEAD", "--worktree", "--", *written, cwd=root)
        for name in remove_new_ignored(root, built_before):
            say(f"removed build output {name}")
        drop_scratch(scratch, args.keep_scratch)


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


def dispatch_and_wait(github: GitHubRoute, version: str, commit: str, *, root: Path = REPOSITORY) -> None:
    started = time.time()
    github.gh(["workflow", "run", WORKFLOW, "--ref", version,
               "-f", f"package={SET_INPUT}", "-f", f"expected_version={version}"], cwd=root)
    run_id = ""
    for _ in range(30):
        time.sleep(5)
        listed = json.loads(github.gh(["run", "list", "--workflow", WORKFLOW, "--limit", "10",
                                       "--json", "databaseId,headBranch,headSha,event,createdAt"], cwd=root).stdout)
        run_id = matching_run(listed, version, commit, started)
        if run_id:
            break
    if not run_id:
        raise ReleaseError("the publish run did not appear; check the Actions tab")
    say(f"publish run {run_id}: waiting")
    result = github.gh(["run", "watch", run_id, "--exit-status", "--interval", "30"], cwd=root, capture=False, check=False)
    if result.returncode != 0:
        raise ReleaseError(f"publish run {run_id} failed; the packages after the failed one were not published")


def matching_run(listed: list[dict], version: str, commit: str, started: float) -> str:
    """The run this dispatch started: the tag, the tagged commit, a dispatch, and not older than the request."""

    for row in listed:
        created = calendar.timegm(time.strptime(row["createdAt"], "%Y-%m-%dT%H:%M:%SZ"))
        if (row["headBranch"] == version and row.get("headSha") == commit
                and row["event"] == "workflow_dispatch" and created >= started - 60):
            return str(row["databaseId"])
    return ""


def publish(args: argparse.Namespace) -> int:
    git("fetch", "--quiet", "--tags", "origin")
    if tag_exists(args.version):
        raise ReleaseError(f"tag {args.version} already exists on origin; run verify, or cut a new version")
    commit = release_commit(args.version)
    say(f"release commit {commit[:12]} on origin/main")
    if args.dry_run:
        say(f"dry run: would tag {args.version} at {commit[:12]}, dispatch {WORKFLOW} package={SET_INPUT}, wait, then verify")
        return 0
    github = github_route(args)
    git("tag", "-a", args.version, "-m", f"pb set {args.version}", commit)
    github.push("origin", [f"refs/tags/{args.version}"], cwd=REPOSITORY)
    dispatch_and_wait(github, args.version, commit)
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
    scratch = scratch_folder(args, "verify")
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
        drop_scratch(scratch, getattr(args, "keep_scratch", False))
    print(f"\nHosts move to it with:\n\n  pb source use-release --expect-version {version}\n")
    return 0


def add_scratch(step: argparse.ArgumentParser) -> None:
    step.add_argument("--scratch", required=True,
                      help="folder for the environments and distributions, e.g. a `pb worker scratch --new` run; "
                           "the run makes and removes only its own subfolder there")
    step.add_argument("--keep-scratch", action="store_true", help="leave the run's subfolder for inspection")


def add_github_route(step: argparse.ArgumentParser) -> None:
    route = step.add_argument_group("governed GitHub route (pb worker gh / pb worker push; required unless --dry-run)")
    route.add_argument("--project-ref", help="the project whose owner GitHub key the release uses")
    route.add_argument("--github-login", help="the login that key must answer as; checked before every push")
    route.add_argument("--runtime-kind", help="passed to pb worker (Claude Code: claude-code)")
    route.add_argument("--runtime-session-id", help="passed to pb worker (Claude Code: this session's id)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts/release-pb", description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    step = commands.add_parser("prepare", help="set the version, run the gate, open the release pull request")
    step.add_argument("version")
    step.add_argument("--notes", required=True, help="file with the changes, written into each release record")
    step.add_argument("--dry-run", action="store_true",
                      help="do everything in the release tree, then put its files back; commit nothing")
    step.add_argument("--skip-gate", action="store_true", help=argparse.SUPPRESS)
    step.add_argument("--python", help="interpreter for the gate environments (default: python3.11)")
    add_scratch(step)
    add_github_route(step)
    step.set_defaults(handler=prepare)
    step = commands.add_parser("publish", help="after the release pull request is merged: tag, publish, verify")
    step.add_argument("version")
    step.add_argument("--dry-run", action="store_true", help="find the release commit and say what would happen")
    step.add_argument("--python", help="interpreter for the verification environment")
    add_scratch(step)
    add_github_route(step)
    step.set_defaults(handler=publish)
    step = commands.add_parser("verify", help="check PyPI and a clean install of project-board==<version>")
    step.add_argument("version")
    step.add_argument("--python", help="interpreter for the verification environment")
    add_scratch(step)
    step.set_defaults(handler=verify)
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except ReleaseError as exc:
        print(f"release-pb: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
