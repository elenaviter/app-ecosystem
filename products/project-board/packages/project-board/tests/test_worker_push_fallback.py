"""`pb worker push`: the deploy key when the owner's key is unavailable, never after a refusal (W371).

Seen live during the 17:21Z platform rebuild: with clones on HTTPS over the
owner's key, `git push` failed ("pb GitHub key: http_502", then "could not
read Username") because Connection Hub was down, and every agent on the
machine was blocked. A credential helper cannot move a push to SSH, so the
fallback is this command: the push as given, and when it fails after an
availability failure of the key, the same push through `deploykey`.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from project_board.client import cli
from project_board.client.store import SharedFieldStore
from test_workspace_report import _git, _remote

PROJECT_ID = "demo-project-0a1b2c3d"
REPO = "example-org/app-ecosystem"


def _setup(tmp_path: Path, monkeypatch, *, code: str, availability: bool, age_seconds: int = 5):
    deploy = _remote(tmp_path / "remotes", "deploy")
    clone = tmp_path / "clone"
    _git("clone", "-q", str(deploy), str(clone), cwd=tmp_path)
    # origin fails like an HTTPS push with no credential; deploykey is this machine's deploy route.
    _git("remote", "set-url", "origin", str(tmp_path / "unreachable.git"), cwd=clone)
    _git("remote", "add", "deploykey", str(deploy), cwd=clone)
    _git("checkout", "-q", "-b", "work/fallback", cwd=clone)
    (clone / "NEW.md").write_text("new", encoding="utf-8")
    _git("add", "NEW.md", cwd=clone)
    _git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "new", cwd=clone)

    field = SharedFieldStore(tmp_path / "field")
    field.create_project(project_id=PROJECT_ID, title="Demo", goal="Demo", owner="control-plane")
    field.record_github_key_outcome("worker-one", PROJECT_ID, REPO, code=code, availability=availability)
    if age_seconds:
        path = next((tmp_path / "field").rglob("github-key-outcome/worker-one.json"))
        import json
        record = json.loads(path.read_text(encoding="utf-8"))
        at = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
        record[REPO]["at"] = at.isoformat().replace("+00:00", "Z")
        path.write_text(json.dumps(record), encoding="utf-8")

    session = SimpleNamespace(
        field=field, identity=SimpleNamespace(worker_name="worker-one"), project_ref=f"work:project:{PROJECT_ID}"
    )
    monkeypatch.setattr(cli, "_GitHubKeySession", lambda args: session)
    monkeypatch.setattr(cli, "repository_name", lambda url: REPO)
    monkeypatch.chdir(clone)
    return deploy, clone


def _branch(repo: Path) -> str:
    found = subprocess.run(["git", "rev-parse", "--verify", "-q", "refs/heads/work/fallback"], cwd=str(repo),
                           capture_output=True, text=True)
    return found.stdout.strip()


def _push():
    return cli._push_command(SimpleNamespace(git_args=["--", "origin", "work/fallback"]))  # noqa: SLF001


def test_an_unavailable_key_pushes_through_the_deploy_key_and_says_so(tmp_path, monkeypatch, capsys):
    deploy, clone = _setup(tmp_path, monkeypatch, code="http_502", availability=True)

    assert _push() == 0
    assert _branch(deploy) == subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(clone), capture_output=True,
                                             text=True, check=True).stdout.strip()
    assert "owner key unavailable (http_502): pushed with the deploy key" in capsys.readouterr().err


@pytest.mark.parametrize("code, availability, age", [("card_denies", False, 5), ("http_502", True, 3600)])
def test_a_refusal_or_a_stale_failure_never_falls_back(tmp_path, monkeypatch, capsys, code, availability, age):
    deploy, _clone = _setup(tmp_path, monkeypatch, code=code, availability=availability, age_seconds=age)

    assert _push() != 0
    assert _branch(deploy) == ""
    assert "deploy key" not in capsys.readouterr().err


# W454: a release must push with the owner's key or not at all. The default
# above stays as it is; `--owner-key-only` decides before anything is written.


def _owner_only_push(*git_args: str):
    return cli._push_command(SimpleNamespace(git_args=["--", *git_args], owner_key_only=True))  # noqa: SLF001


def test_owner_key_only_never_falls_back_after_an_availability_failure(tmp_path, monkeypatch, capsys):
    deploy, clone = _setup(tmp_path, monkeypatch, code="http_502", availability=True)
    # An HTTPS origin that refuses the connection, as when the owner key's route is down.
    _git("remote", "set-url", "origin", "https://127.0.0.1:9/example-org/app-ecosystem.git", cwd=clone)
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")

    assert _owner_only_push("origin", "work/fallback") != 0
    assert _branch(deploy) == "", "the deploy-key remote received nothing"
    err = capsys.readouterr().err
    assert "not retried through the deploy key" in err
    assert "pushed with the deploy key" not in err


@pytest.mark.parametrize("url", ["git@github.com:example-org/app-ecosystem.git", "LOCAL"])
def test_owner_key_only_refuses_a_remote_that_does_not_push_over_https(tmp_path, monkeypatch, capsys, url):
    deploy, clone = _setup(tmp_path, monkeypatch, code="http_502", availability=True)
    if url == "LOCAL":
        # A plain path pushes without any credential at all.
        url = str(deploy)
    _git("remote", "set-url", "origin", url, cwd=clone)

    assert _owner_only_push("origin", "work/fallback") == 2
    assert _branch(deploy) == "", "nothing was pushed, not even to the local path"
    assert "pushes over HTTPS only" in capsys.readouterr().err


def test_owner_key_only_checks_every_push_url_and_needs_the_remote_named(tmp_path, monkeypatch, capsys):
    deploy, clone = _setup(tmp_path, monkeypatch, code="http_502", availability=True)
    _git("remote", "set-url", "origin", "https://127.0.0.1:9/example-org/app-ecosystem.git", cwd=clone)
    _git("remote", "set-url", "--add", "--push", "origin", "https://127.0.0.1:9/example-org/app-ecosystem.git", cwd=clone)
    _git("remote", "set-url", "--add", "--push", "origin", str(deploy), cwd=clone)

    assert _owner_only_push("origin", "work/fallback") == 2
    assert _owner_only_push("work/fallback") == 2
    assert _owner_only_push() == 2
    assert _branch(deploy) == ""
    assert "needs the remote named first" in capsys.readouterr().err


def test_the_default_push_keeps_its_fallback_and_the_parser_offers_the_mode():
    parser = cli.build_parser()
    args = parser.parse_args(["worker", "push", "--owner-key-only", "--", "origin", "main"])
    assert args.owner_key_only is True
    assert parser.parse_args(["worker", "push", "--", "origin", "main"]).owner_key_only is False
