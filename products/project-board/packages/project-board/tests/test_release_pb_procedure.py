"""The pb set's one-command release (W364).

Operator, 2026-09-26: "simple release procedure for us". The set
(app-foundation, service-foundation, connection-hub, project-board) is
released at one version by `scripts/release-pb`: prepare sets the version in
every file and gates each package the way the publish workflow does, publish
tags the release merge and runs the workflow once for the whole set, verify
checks PyPI and a clean install. The 2026.09.26.2205 attempt published the
foundations and stopped at connection-hub; these tests keep that from
recurring silently.
"""

from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[5]
SCRIPT = REPOSITORY_ROOT / "scripts" / "release_pb.py"
WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "publish-python-package.yml"


def _script():
    spec = importlib.util.spec_from_file_location("release_pb", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["release_pb"] = module
    spec.loader.exec_module(module)
    return module


release_pb = _script()


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.test", *args],
        cwd=root, check=True, capture_output=True, text=True,
    ).stdout.strip()


def _copy_of_the_set(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    for relative in release_pb.VERSION_ROOTS:
        source = REPOSITORY_ROOT / relative
        target = root / relative
        if source.is_dir():
            shutil.copytree(source, target, ignore=shutil.ignore_patterns("node_modules", "__pycache__", "*.egg-info", "build", "dist"))
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "set")
    return root


def test_prepare_sets_the_new_version_everywhere_and_leaves_no_old_one(tmp_path: Path) -> None:
    root = _copy_of_the_set(tmp_path)
    current = release_pb.read_version(root)
    new = "2099.01.02.0304"

    changed = release_pb.apply_version(root, current, new, "What changed.\n\n- one\n- two")

    assert release_pb.version_files(root, current) == []
    names = {str(path.relative_to(root)) for path in changed}
    for package in release_pb.SET:
        assert f"{package.path}/pyproject.toml" in names
        pyproject = (root / package.path / "pyproject.toml").read_text(encoding="utf-8")
        assert f'version = "{new}"' in pyproject
    # The set depends on itself at the new version, and the chain test moves with it.
    project_board = (root / "products/project-board/packages/project-board/pyproject.toml").read_text(encoding="utf-8")
    assert f"connection-hub[client]>={new}" in project_board
    chain = (root / "products/project-board/packages/project-board/tests/test_release_dependency_chain.py").read_text(encoding="utf-8")
    assert f'RELEASE_VERSION = "{new}"' in chain
    for record in release_pb.RELEASE_RECORDS:
        data = yaml.safe_load((root / record).read_text(encoding="utf-8"))
        top = data.get("package") or data.get("product")
        assert top["ref"] == new, record
        assert top["description"] == "What changed.\n\n- one\n- two\n", record
    product = yaml.safe_load((root / "products/connection-hub/release.yaml").read_text(encoding="utf-8"))
    assert product["config"]["version"] == new
    assert product["components"]["python_package"]["version"] == new


def test_the_sweep_moves_only_the_cli_floors_never_its_version_or_readme(tmp_path: Path) -> None:
    """W1016: the CLI is versioned on its own; #940 had to restore its README floor by hand."""

    root = _copy_of_the_set(tmp_path)
    current = release_pb.read_version(root)
    cli = root / "products/connection-hub/packages/connection-hub-cli"
    readme = cli / "README.md"
    readme.write_text(f"The published CLI installs the App Ecosystem packages at {current} or later.\n",
                      encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "readme")
    cli_pyproject = "products/connection-hub/packages/connection-hub-cli/pyproject.toml"

    def cli_metadata() -> dict:
        return tomllib.loads((root / cli_pyproject).read_text(encoding="utf-8"))["project"]

    cli_version = cli_metadata()["version"]
    new = "2099.01.02.0304"

    changed = release_pb.apply_version(root, current, new, "What changed.")

    names = {str(path.relative_to(root)) for path in changed}
    assert readme.read_text(encoding="utf-8").count(current) == 1, "the sweep rewrote the published CLI's README"
    assert "products/connection-hub/packages/connection-hub-cli/README.md" not in names
    assert cli_pyproject in names
    assert cli_metadata()["version"] == cli_version
    assert f"connection-hub[client]>={new}" in (root / cli_pyproject).read_text(encoding="utf-8")
    product = yaml.safe_load((root / "products/connection-hub/release.yaml").read_text(encoding="utf-8"))
    assert product["components"]["cli_package"]["version"] == cli_version


def test_the_sweep_refuses_to_move_the_cli_version(tmp_path: Path) -> None:
    root = _copy_of_the_set(tmp_path)
    current = release_pb.read_version(root)
    pyproject = root / release_pb.CLI_PYPROJECT
    text = pyproject.read_text(encoding="utf-8")
    pyproject.write_text(text.replace(f'version = "{release_pb.read_cli_version(root)}"', f'version = "{current}"'),
                         encoding="utf-8")
    with pytest.raises(release_pb.ReleaseError, match="a CLI release sets it"):
        release_pb.apply_version(root, current, "2099.01.02.0304", "What changed.")


def test_the_release_opening_says_the_cli_is_not_released() -> None:
    line = release_pb.cli_boundary("2099.01.02.0304", "2099.01.01.0100")
    assert "not released here" in line and "2099.01.01.0100" in line and ">=2099.01.02.0304" in line
    assert "carries" not in line
    assert "carries the version" not in SCRIPT.read_text(encoding="utf-8")
    guide = (REPOSITORY_ROOT / "docs" / "releases.md").read_text(encoding="utf-8")
    assert "carries the\nsame version" not in guide and "is not part\nof the set" in guide


# The versions below are fixed and never the set's current one: `prepare`
# rewrites every file of the set that names the current version.


def test_a_version_must_be_the_calendar_form_and_newer() -> None:
    release_pb.check_new_version("2000.01.01.0000", "2000.01.02.0100")
    with pytest.raises(release_pb.ReleaseError, match="YYYY.MM.DD.HHMM"):
        release_pb.check_new_version("2000.01.01.0000", "2026.9.27.100")
    with pytest.raises(release_pb.ReleaseError, match="not newer"):
        release_pb.check_new_version("2000.01.01.0000", "2000.01.01.0000")
    assert release_pb.canonical("2000.01.02.0100") == "2000.1.2.100"


def test_publish_tags_the_merge_that_brought_the_version_not_later_main(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    pyproject = root / "products/project-board/packages/project-board/pyproject.toml"
    pyproject.parent.mkdir(parents=True)
    _git(root.parent, "init", "-q", "-b", "main", str(root))

    def commit(version: str, message: str) -> str:
        pyproject.write_text(f'[project]\nname = "project-board"\nversion = "{version}"\n', encoding="utf-8")
        (root / "other.txt").write_text(message, encoding="utf-8")
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", message)
        return _git(root, "rev-parse", "HEAD")

    commit("2000.01.01.0000", "before")
    release = commit("2000.01.02.0100", "release")
    commit("2000.01.02.0100", "later work on main")

    assert release_pb.release_commit("2000.01.02.0100", cwd=root, ref="main") == release
    with pytest.raises(release_pb.ReleaseError, match="does not carry"):
        release_pb.release_commit("2000.01.03.0100", cwd=root, ref="main")


def test_the_workflow_publishes_the_set_in_order_and_stops_at_a_failure() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    triggers = workflow.get("on") or workflow.get(True)
    assert "pb-set" in triggers["workflow_dispatch"]["inputs"]["package"]["options"]
    job = workflow["jobs"]["build-and-publish"]
    assert job["strategy"]["max-parallel"] == 1
    assert job["strategy"]["fail-fast"] is True
    matrix = job["strategy"]["matrix"]["package"]
    order = '","'.join(package.name for package in release_pb.SET)
    assert f'\'["{order}"]\'' in matrix
    steps = [step["name"] for step in job["steps"] if "name" in step]
    assert steps.index("Wait for first-party dependencies of this version on the index") < steps.index("Test package")
    assert steps[-1] == "Publish to PyPI"
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "${{ inputs.package }}" not in text.split("jobs:", 1)[1].replace(
        "format('[\"{0}\"]', inputs.package)", ""
    ).replace("inputs.package == 'pb-set'", ""), "steps read the matrix package"
    for package in release_pb.SET:
        assert f'"{package.path}"' in text and f'"{package.import_name}"' in text, package.name


def _publish_wait():
    spec = importlib.util.spec_from_file_location("publish_wait", REPOSITORY_ROOT / "scripts" / "publish_wait.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


publish_wait = _publish_wait()


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def test_the_publish_wait_needs_consecutive_resolutions_of_the_install() -> None:
    """W1016, run 42207266620: one success then a stale edge must not reach the tests."""

    answers = iter([False, True, False, True, True, True])
    probed = []
    clock = _Clock()

    def probe(target: str, timeout: float) -> bool:
        probed.append(target)
        return next(answers)

    count = publish_wait.wait("pkg[test]", ["connection-hub"], probe=probe, clock=clock, sleep=clock.sleep,
                              say=lambda _: None)

    assert count == 6
    assert probed == ["pkg[test]"] * 6


def test_the_publish_wait_fails_the_step_when_the_install_never_resolves() -> None:
    """The index-lag case: the file is on the index, the install does not resolve, Test package is not reached."""

    clock = _Clock()
    with pytest.raises(SystemExit, match="did not resolve connection-hub 3 times in a row within 20 minutes"):
        publish_wait.wait("pkg[test]", ["connection-hub"], probe=lambda _target, _timeout: False, clock=clock,
                          sleep=clock.sleep, say=lambda _: None)
    assert clock.now == 20 * 60  # it slept up to the deadline, not past it


def test_the_publish_wait_refuses_a_success_that_ends_after_its_deadline() -> None:
    """Review of #941 at 924e6175: a third success ending at 1.2 s of a 1 s budget was accepted."""

    clock = _Clock()

    def slow(target: str, timeout: float | None = None) -> bool:  # accepts the old one-argument probe too
        clock.now += 0.4
        return True

    with pytest.raises(SystemExit, match="did not resolve"):
        publish_wait.wait("pkg", ["connection-hub"], probe=slow, needed=3, interval=0, limit=1,
                          clock=clock, sleep=clock.sleep, say=lambda _: None)


def test_each_probe_and_sleep_stays_inside_the_remaining_budget() -> None:
    clock = _Clock()
    timeouts, sleeps = [], []

    def probe(target: str, timeout: float) -> bool:
        timeouts.append(timeout)
        clock.now += 7
        return False

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock.sleep(seconds)

    with pytest.raises(SystemExit):
        publish_wait.wait("pkg", ["connection-hub"], probe=probe, interval=20, limit=60, probe_limit=30,
                          clock=clock, sleep=sleep, say=lambda _: None)
    assert timeouts[0] == 30  # capped by probe_limit
    assert all(0 < value <= 30 for value in timeouts)
    assert timeouts[-1] < 30  # the last probe gets only what is left
    assert all(seconds >= 0 for seconds in sleeps)
    assert clock.now <= 60 + 7  # nothing starts after the deadline


def test_a_pip_child_that_times_out_is_a_failed_check(monkeypatch) -> None:
    seen = {}

    def hung(args, **kwargs):
        seen.update(kwargs)
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    monkeypatch.setattr(publish_wait.subprocess, "run", hung)
    assert publish_wait.resolves("pkg[test]", 12.5) is False
    assert seen["timeout"] == 12.5


def test_the_publish_wait_names_the_set_floors_and_installs_what_the_tests_install() -> None:
    project_board = REPOSITORY_ROOT / "products/project-board/packages/project-board"
    version = release_pb.read_version(REPOSITORY_ROOT)
    assert {"app-foundation", "connection-hub", "service-foundation"} <= set(
        publish_wait.waiting_for(project_board / "pyproject.toml", version)
    )
    assert publish_wait.waiting_for(project_board / "pyproject.toml", "2000.01.01.0100") == []
    assert publish_wait.install_target("p", True) == "p[test]"
    assert publish_wait.install_target("p", False) == "p"
    assert publish_wait.wait("p", [], probe=lambda _target, _timeout: pytest.fail("no probe"), say=lambda _: None) == 0


# PENDING (W1016): the board's GitHub App cannot push workflow files, so the
# workflow change waits on the operator's choice (grant the permission, or
# apply the patch herself). strict=True turns this into a failure the moment
# the workflow calls the script, so the marker cannot outlive the change.
@pytest.mark.xfail(strict=True, reason="PENDING W1016: workflow change awaits the operator (workflows permission)")
def test_the_publish_workflow_waits_with_the_install_the_tests_run() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = {step.get("name"): step for step in workflow["jobs"]["build-and-publish"]["steps"]}
    wait_run = steps["Wait for first-party dependencies of this version on the index"]["run"]
    test_run = steps["Test package"]["run"]
    assert "python scripts/publish_wait.py" in wait_run and "pip download" not in wait_run
    # The wait resolves the [test] extra exactly for the packages whose tests install it.
    condition = re.compile(r"^\s*if (.+); then$", re.M)
    assert condition.search(wait_run).group(1) == condition.search(test_run).group(1)
    assert 'tested="--tested"' in wait_run and "$tested" in wait_run


def test_the_release_guide_leads_with_the_procedure() -> None:
    guide = (REPOSITORY_ROOT / "docs" / "releases.md").read_text(encoding="utf-8")
    first_section = guide.split("\n## ", 2)[1]
    assert first_section.startswith("Release the pb set")
    for command in ("scripts/release-pb prepare", "scripts/release-pb publish", "scripts/release-pb verify",
                    "pb source use-release --expect-version"):
        assert command in first_section, command
    # The repository's procedure names neutral roles; a project's own roles
    # (its coordinator, its operator) belong on that project's pages (operator, 2026-09-27).
    for project_role in ("coordinator", "operator"):
        assert project_role not in first_section.lower(), project_role
    assert "The maintainer" in first_section and "release owner's approval" in first_section


# W454: a release reaches GitHub only through the agent's governed route, and
# it works in a tree and a scratch folder the workspace accounts for. The
# helper used the ambient `gh` login and hidden temporary worktrees before.


def test_the_script_has_no_ambient_gh_and_no_hidden_trees() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    code = source.split('"""', 2)[2]
    assert 'run(["gh"' not in code and "run(['gh'" not in code
    assert '"gh", "' not in code.replace('self.command("gh", args)', "")
    assert "import tempfile" not in code and "mkdtemp" not in code
    assert "--force" not in code
    assert 'git("push"' not in code, "a push goes through GitHubRoute.push"


def _fake_pb(tmp_path: Path, *, login: str = "owner", push_stderr: str = "", pr_view: str = "{}") -> tuple[str, Path]:
    log = tmp_path / "pb-calls.txt"
    fake = tmp_path / "pb"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"open({str(log)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "args = sys.argv[sys.argv.index('--') + 1:]\n"
        "if sys.argv[2] == 'push':\n"
        f"    sys.stderr.write({push_stderr!r}); sys.exit(0)\n"
        "if args[:2] == ['api', 'user']:\n"
        f"    print({login!r}); sys.exit(0)\n"
        "if args[:2] == ['pr', 'create']:\n"
        "    print('https://github.example/o/r/pull/1'); sys.exit(0)\n"
        "if args[:2] == ['pr', 'view']:\n"
        f"    print({pr_view!r}); sys.exit(0)\n"
        "sys.exit(3)\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    return str(fake), log


def _calls(log: Path) -> list[list[str]]:
    import json

    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def _clone(tmp_path: Path, **remotes: str) -> Path:
    root = tmp_path / "clone"
    _git(tmp_path, "init", "-q", "-b", "main", str(root))
    for name, url in remotes.items():
        _git(root, "remote", "add", name, url)
    return root


def test_a_push_names_the_project_and_checks_the_actor_before_it_writes(tmp_path: Path) -> None:
    fake, log = _fake_pb(tmp_path)
    root = _clone(tmp_path, origin="https://github.com/o/r.git")
    route = release_pb.GitHubRoute("work:project:p", "owner", ("--runtime-kind", "claude-code"), pb=fake)

    route.push("origin", ["release/2099.01.02.0304"], cwd=root)

    actor, push = _calls(log)
    assert actor == ["worker", "gh", "--project-ref", "work:project:p", "--runtime-kind", "claude-code",
                     "--", "api", "user", "--jq", ".login"]
    assert push == ["worker", "push", "--project-ref", "work:project:p", "--runtime-kind", "claude-code",
                    "--owner-key-only", "--", "--quiet", "origin", "release/2099.01.02.0304"]


def test_a_push_uses_the_owner_key_only_mode_even_where_a_deploy_key_remote_exists(tmp_path: Path) -> None:
    """`pb worker push --owner-key-only` never falls back, so the remote may stay."""

    fake, log = _fake_pb(tmp_path)
    route = release_pb.GitHubRoute("work:project:p", "owner", pb=fake)
    root = _clone(tmp_path, origin="https://github.com/o/r.git", deploykey="github-r:o/r.git")

    route.push("origin", ["x"], cwd=root)

    assert "--owner-key-only" in _calls(log)[-1]


def test_a_push_fails_closed_on_a_remote_that_is_not_https(tmp_path: Path) -> None:
    fake, log = _fake_pb(tmp_path)
    route = release_pb.GitHubRoute("work:project:p", "owner", pb=fake)
    root = _clone(tmp_path, origin="git@github.com:o/r.git")
    with pytest.raises(release_pb.ReleaseError, match="over HTTPS with the owner key only"):
        route.push("origin", ["x"], cwd=root)
    assert not log.exists(), "nothing ran, not even the actor check"


def test_a_push_that_still_reports_the_deploy_key_stops_the_release(tmp_path: Path) -> None:
    fake, _ = _fake_pb(tmp_path, push_stderr="pb GitHub key: owner key unavailable (x): pushed with the deploy key\n")
    route = release_pb.GitHubRoute("work:project:p", "owner", pb=fake)
    with pytest.raises(release_pb.ReleaseError, match="deploy-key fallback"):
        route.push("origin", ["x"], cwd=_clone(tmp_path, origin="https://github.com/o/r.git"))


def test_a_push_does_not_run_when_github_answers_as_someone_else(tmp_path: Path) -> None:
    fake, log = _fake_pb(tmp_path, login="someone-else")
    route = release_pb.GitHubRoute("work:project:p", "owner", pb=fake)

    with pytest.raises(release_pb.ReleaseError, match="not 'owner'; nothing was pushed"):
        route.push("origin", ["x"], cwd=_clone(tmp_path, origin="https://github.com/o/r.git"))
    assert [call[1] for call in _calls(log)] == ["gh"]


def test_the_release_pull_request_must_hold_the_gated_commit(tmp_path: Path) -> None:
    import json

    root = tmp_path / "repo"
    _git(tmp_path, "init", "-q", "-b", "main", str(root))
    _git(root, "commit", "-q", "--allow-empty", "-m", "release")
    head = _git(root, "rev-parse", "HEAD")
    good = {"headRefName": "release/2099.01.02.0304", "baseRefName": "main", "headRefOid": head}

    fake, _ = _fake_pb(tmp_path, pr_view=json.dumps(good))
    route = release_pb.GitHubRoute("work:project:p", "owner", pb=fake)
    assert release_pb.open_release_pull_request(route, "2099.01.02.0304", "body", root=root).endswith("/pull/1")

    fake, _ = _fake_pb(tmp_path, pr_view=json.dumps({**good, "headRefOid": "0" * 40}))
    route = release_pb.GitHubRoute("work:project:p", "owner", pb=fake)
    with pytest.raises(release_pb.ReleaseError, match="not the gated release"):
        release_pb.open_release_pull_request(route, "2099.01.02.0304", "body", root=root)


def test_the_publish_run_is_the_one_this_dispatch_started() -> None:
    started = 4_000_000_000.0  # 2096-10-02T07:06:40Z
    when = "2096-10-02T07:06:40Z"
    row = {"databaseId": 7, "headBranch": "2099.01.02.0304", "headSha": "abc", "event": "workflow_dispatch",
           "createdAt": when}
    assert release_pb.matching_run([row], "2099.01.02.0304", "abc", started) == "7"
    assert release_pb.matching_run([{**row, "headSha": "other"}], "2099.01.02.0304", "abc", started) == ""
    assert release_pb.matching_run([{**row, "event": "push"}], "2099.01.02.0304", "abc", started) == ""
    assert release_pb.matching_run([row], "2099.01.02.0304", "abc", started + 3600) == ""


def test_a_real_prepare_or_publish_refuses_without_the_governed_route(tmp_path: Path, capsys) -> None:
    notes = tmp_path / "notes.md"
    notes.write_text("What changed.", encoding="utf-8")
    assert release_pb.main(["prepare", "2099.01.02.0304", "--notes", str(notes), "--scratch", str(tmp_path)]) == 1
    assert "--project-ref <project> --github-login" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == [notes], "nothing was made before the refusal"


def test_the_release_runs_only_in_its_own_linked_worktree(tmp_path: Path) -> None:
    clone = tmp_path / "clone"
    _git(tmp_path, "init", "-q", "-b", "main", str(clone))
    _git(clone, "commit", "-q", "--allow-empty", "-m", "base")
    with pytest.raises(release_pb.ReleaseError, match="main checkout"):
        release_pb.require_own_worktree(clone)
    tree = tmp_path / "wt" / "release-2099.01.02.0304"
    _git(clone, "worktree", "add", "-q", "--detach", str(tree), "HEAD")
    release_pb.require_own_worktree(tree)


def test_cleanup_removes_only_the_build_outputs_this_run_made(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    package = root / release_pb.SET[0].path
    package.mkdir(parents=True)
    (root / ".gitignore").write_text("*.egg-info/\n__pycache__/\nnotes.local\n", encoding="utf-8")
    (package / "keep.py").write_text("", encoding="utf-8")
    _git(tmp_path, "init", "-q", "-b", "main", str(root))
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    def made(path: Path) -> None:
        path.mkdir()
        (path / "PKG-INFO").write_text("x", encoding="utf-8")

    made(package / "old.egg-info")
    (root / "notes.local").write_text("mine", encoding="utf-8")
    before = release_pb.ignored_paths(root)

    made(package / "new.egg-info")
    made(package / "__pycache__")
    made(root / "elsewhere.egg-info")
    removed = release_pb.remove_new_ignored(root, before)

    prefix = release_pb.SET[0].path
    assert removed == [f"{prefix}/__pycache__/", f"{prefix}/new.egg-info/"]
    assert (package / "old.egg-info").is_dir() and (root / "notes.local").exists()
    assert (root / "elsewhere.egg-info").is_dir(), "outside the set's packages nothing is touched"


def test_the_scratch_subfolder_is_new_and_the_only_thing_removed(tmp_path: Path) -> None:
    import argparse

    args = argparse.Namespace(scratch=str(tmp_path), version="2099.01.02.0304")
    (tmp_path / "earlier-finding.txt").write_text("keep", encoding="utf-8")
    folder = release_pb.scratch_folder(args, "prepare")
    assert folder == tmp_path / "release-pb-prepare-2099.01.02.0304"
    with pytest.raises(release_pb.ReleaseError, match="exists from an earlier run"):
        release_pb.scratch_folder(args, "prepare")
    release_pb.drop_scratch(folder, keep=False)
    assert [path.name for path in tmp_path.iterdir()] == ["earlier-finding.txt"]
    with pytest.raises(release_pb.ReleaseError, match="not a folder"):
        release_pb.scratch_folder(argparse.Namespace(scratch=str(tmp_path / "missing"), version="x"), "verify")


def test_the_release_guide_names_the_governed_route_and_the_release_tree() -> None:
    guide = (REPOSITORY_ROOT / "docs" / "releases.md").read_text(encoding="utf-8")
    first_section = guide.split("\n## ", 2)[1]
    for phrase in ("wt/release-<version>", "--scratch", "--project-ref", "--github-login",
                   "pb worker gh", "pb worker push --owner-key-only", "HTTPS"):
        assert phrase in first_section, phrase
    assert "throwaway worktree" not in first_section


def test_a_release_leaves_the_procedure_package_and_its_digest_alone(tmp_path: Path) -> None:
    """The 2026-10-01 dry run rewrote a version named in project-workspace.md.

    The procedure is versioned by its revision ledger; a changed digest under
    a recorded revision makes `pb procedure verify` fail on every host. The
    gate now requires the recorded revision, so such a change fails it.
    """

    root = _copy_of_the_set(tmp_path)
    procedures = root / release_pb.VERSION_EXCLUDED[0]
    current = release_pb.read_version(root)
    # A procedure page that names the release a behaviour came with.
    (procedures / "named-release.md").write_text(f"Since {current}.\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "a page names the release")
    before = {path: path.read_bytes() for path in procedures.rglob("*") if path.is_file()}

    changed = release_pb.apply_version(root, current, "2099.01.02.0304", "What changed.")

    assert not [path for path in changed if str(path).startswith(str(procedures))]
    assert {path: path.read_bytes() for path in procedures.rglob("*") if path.is_file()} == before
    assert release_pb.clean_env()["PB_REQUIRE_REVISION_RECORDED"] == "1"
