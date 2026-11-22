"""W762: PB's own side forces the account the operator's Card names.

Operator, 2026-10-12: "Let them always act according to what is configured in the pb for that project, on operator
card"; "for pb workers their side must be forced to work with the account as configured in connection hub for the
operator card"; "this should not depend on agents." Every check here is relative to the Card's answer: no account
name is fixed, and one Card never stamps another project's clone.
"""

from __future__ import annotations

import asyncio
import hashlib
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from project_board.client import cli
from project_board.client.github_key import GitHubKeyRefused, GitHubToken, clone_config, verified_login

PROJECT = "work:project:quickstart-demo"
OTHER = "work:project:other-demo"
REPO = "example-org/applications"
HELPER = "!pb worker git-credential --runtime-kind claude-code --runtime-session-id s1"


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True).stdout.strip()


def _config(folder: Path, key: str) -> str:
    found = subprocess.run(["git", "config", "--local", "--get", key], cwd=str(folder), capture_output=True, text=True)
    return found.stdout.strip()


@pytest.fixture(autouse=True)
def _isolated_git(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-global"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def _clone(tmp_path: Path, name: str = "clone", *, origin: str = f"https://github.com/{REPO}.git",
           helper: str = HELPER) -> Path:
    folder = tmp_path / name
    folder.mkdir()
    _git("init", "-q", "-b", "main", cwd=folder)
    _git("remote", "add", "origin", origin, cwd=folder)
    if helper:
        _git("config", "--replace-all", "credential.https://github.com.helper", "", cwd=folder)
        _git("config", "--add", "credential.https://github.com.helper", helper, cwd=folder)
    _git("config", "user.name", "agent@host", cwd=folder)
    _git("config", "user.email", "old@example.test", cwd=folder)
    (folder / "a.txt").write_text("a\n", encoding="utf-8")
    _git("add", "a.txt", cwd=folder)
    _git("commit", "-q", "-m", "a", cwd=folder)
    return folder


def _token(login: str = "card-account", email: str = "card@example.test", repository: str = REPO) -> GitHubToken:
    return GitHubToken(token="ghu_never_printed", expires_at=0, login=login, commit_email=email, repository=repository)


def _digest(folder: Path) -> str:
    parts = [
        _git("status", "--porcelain=v2", "--untracked-files=all", cwd=folder),
        _git("diff", cwd=folder),
        _git("diff", "--cached", cwd=folder),
        _git("rev-parse", "HEAD", cwd=folder),
        _config(folder, "user.name"),
    ]
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()


def test_clone_config_stamps_the_cards_account_and_project_only_together():
    steps = clone_config(HELPER, name="agent", email="card@example.test", account="card-account", project_ref=PROJECT)
    assert ["config", "pb.githubAccount", "card-account"] in steps
    assert ["config", "pb.projectRef", PROJECT] in steps
    assert not [step for step in clone_config(HELPER, email="e@example.test", account="x") if "pb.githubAccount" in step]


@pytest.mark.parametrize("status, body, code", [
    (200, {"login": "someone-else"}, "card_account_mismatch"),
    (401, {}, "card_account_unverified"),
    (403, {"message": "Resource not accessible by integration"}, "card_account_unverified"),
    (200, {}, "card_account_unverified"),
])
def test_the_token_must_act_as_the_cards_account(status, body, code):
    async def get(url, token):
        assert token == "ghu_never_printed" and url.endswith("/user")
        return status, body

    with pytest.raises(GitHubKeyRefused) as refused:
        asyncio.run(verified_login(_token(), get=get))
    assert refused.value.code == code
    assert "ghu_never_printed" not in refused.value.message


def test_a_token_that_acts_as_the_card_passes_and_an_unreachable_github_never_does():
    async def same(url, token):
        return 200, {"login": "Card-Account"}

    async def down(url, token):
        raise ConnectionError("no route")

    assert asyncio.run(verified_login(_token(), get=same)) == "Card-Account"
    with pytest.raises(GitHubKeyRefused) as refused:
        asyncio.run(verified_login(_token(), get=down))
    assert refused.value.code == "card_account_unverified" and refused.value.availability


def test_a_fresh_key_brings_this_projects_clone_to_the_card_and_keeps_its_work(tmp_path):
    clone = _clone(tmp_path)
    (clone / "untracked.txt").write_text("wip\n", encoding="utf-8")
    (clone / "a.txt").write_text("changed\n", encoding="utf-8")
    before = _digest(clone)

    cli._refresh_clone_stamp(PROJECT, _token(), str(clone), helper=HELPER)  # noqa: SLF001

    assert _config(clone, "user.email") == "card@example.test"
    assert _config(clone, "pb.githubAccount") == "card-account"
    assert _config(clone, "pb.projectRef") == PROJECT
    assert _digest(clone) == before, "status, diff, index, HEAD and user.name are unchanged"

    # A changed Card reaches the same clone; the Card is the authority, whatever its account is called.
    cli._refresh_clone_stamp(  # noqa: SLF001
        PROJECT, _token(login="elenaviter", email="personal@example.test"), str(clone), helper=HELPER
    )
    assert (_config(clone, "pb.githubAccount"), _config(clone, "user.email")) == ("elenaviter", "personal@example.test")


def test_one_card_never_stamps_another_projects_clone_or_an_unrelated_repository(tmp_path):
    other = _clone(tmp_path, "other")
    _git("config", "pb.projectRef", OTHER, cwd=other)
    _git("config", "pb.githubAccount", "other-account", cwd=other)
    unrelated = _clone(tmp_path, "unrelated", origin="https://github.com/example-org/elsewhere.git")

    cli._refresh_clone_stamp(PROJECT, _token(), str(other), helper=HELPER)  # noqa: SLF001
    cli._refresh_clone_stamp(PROJECT, _token(), str(unrelated), helper=HELPER)  # noqa: SLF001

    assert (_config(other, "pb.githubAccount"), _config(other, "user.email")) == ("other-account", "old@example.test")
    assert (_config(unrelated, "pb.githubAccount"), _config(unrelated, "user.email")) == ("", "old@example.test")


def test_a_refresh_never_rewrites_a_clone_another_session_answers(tmp_path):
    """Root's review of 2b4a948b: same project and repository is not enough; it must be this worker's clone."""

    theirs = _clone(tmp_path, "theirs", helper=HELPER.replace("s1", "s2"))
    _git("config", "pb.projectRef", PROJECT, cwd=theirs)
    cli._refresh_clone_stamp(PROJECT, _token(), str(theirs), helper=HELPER)  # noqa: SLF001
    assert (_config(theirs, "pb.githubAccount"), _config(theirs, "user.email")) == ("", "old@example.test")
    no_helper = _clone(tmp_path, "no-helper", helper="")
    cli._refresh_clone_stamp(PROJECT, _token(), str(no_helper), helper=HELPER)  # noqa: SLF001
    assert _config(no_helper, "user.email") == "old@example.test"


def test_a_worktree_that_overrides_the_email_is_refused_not_silently_kept(tmp_path):
    clone = _clone(tmp_path)
    _git("config", "extensions.worktreeConfig", "true", cwd=clone)
    _git("config", "--worktree", "user.email", "override@example.test", cwd=clone)

    with pytest.raises(GitHubKeyRefused) as refused:
        cli._refresh_clone_stamp(PROJECT, _token(), str(clone), helper=HELPER)  # noqa: SLF001
    assert refused.value.code == "clone_identity_overridden"


def _stamped(tmp_path: Path) -> Path:
    clone = _clone(tmp_path)
    _git("config", "pb.githubAccount", "card-account", cwd=clone)
    _git("config", "pb.projectRef", PROJECT, cwd=clone)
    _git("config", "user.email", "card@example.test", cwd=clone)
    # What the remote already has: the commit made before the stamp.
    _git("update-ref", "refs/remotes/origin/main", "HEAD", cwd=clone)
    return clone


def _refusal(clone: Path, monkeypatch, *, token: GitHubToken | None = None, verified: Exception | None = None) -> str:
    class Session:
        def __init__(self, args):
            pass

        def helper(self):
            return HELPER

        def token(self, repository):
            return token or _token()

    async def verify(issued, **_):
        if verified:
            raise verified
        return issued.login

    monkeypatch.setattr(cli, "_GitHubKeySession", Session)
    monkeypatch.setattr(cli, "verified_login", verify)
    monkeypatch.chdir(clone)

    def git_out(*command: str) -> str:
        found = subprocess.run(["git", *command], capture_output=True, text=True, check=False)
        return found.stdout.strip() if found.returncode == 0 else ""

    return cli._card_push_refusal(  # noqa: SLF001
        SimpleNamespace(), "origin", f"https://github.com/{REPO}.git", ["origin", "HEAD:refs/heads/w"], git_out
    )


def test_a_push_with_the_cards_account_and_email_is_allowed(tmp_path, monkeypatch):
    clone = _stamped(tmp_path)
    (clone / "b.txt").write_text("b\n", encoding="utf-8")
    _git("add", "b.txt", cwd=clone)
    _git("commit", "-q", "-m", "b", cwd=clone)
    assert _refusal(clone, monkeypatch) == ""


def test_an_unstamped_clone_or_a_second_credential_never_pushes(tmp_path, monkeypatch):
    clone = _stamped(tmp_path)
    _git("config", "--add", "credential.https://github.com.helper", "!gh auth git-credential", cwd=clone)
    assert "clone_credential_override" in _refusal(clone, monkeypatch)

    _git("config", "--unset-all", "credential.https://github.com.helper", "gh auth", cwd=clone)
    assert _refusal(clone, monkeypatch) == ""
    _git("config", "http.https://github.com/.extraheader", "AUTHORIZATION: basic x", cwd=clone)
    assert "clone_credential_override" in _refusal(clone, monkeypatch)

    _git("config", "--unset", "pb.githubAccount", cwd=clone)
    assert "clone_unstamped" in _refusal(clone, monkeypatch)


def test_a_key_that_is_not_the_cards_or_a_clone_that_disagrees_never_pushes(tmp_path, monkeypatch):
    clone = _stamped(tmp_path)
    mismatch = GitHubKeyRefused("card_account_mismatch", "The key acts as someone-else.")
    assert "card_account_mismatch" in _refusal(clone, monkeypatch, verified=mismatch)
    assert "card_account_mismatch" in _refusal(clone, monkeypatch, token=_token(login="another-account"))
    assert "card_account_mismatch" in _refusal(clone, monkeypatch, token=_token(email="another@example.test"))


def test_new_commits_with_another_email_are_named_and_never_pushed(tmp_path, monkeypatch):
    clone = _stamped(tmp_path)
    (clone / "c.txt").write_text("c\n", encoding="utf-8")
    _git("add", "c.txt", cwd=clone)
    _git("-c", "user.email=personal@example.test", "commit", "-q", "-m", "c", cwd=clone)
    refused = _refusal(clone, monkeypatch)
    assert "push_commit_identity_mismatch" in refused and "personal@example.test" in refused


def test_gh_does_not_run_when_the_token_is_not_the_cards(monkeypatch, capsys):
    class Session:
        def __init__(self, args):
            pass

        def token(self, repository):
            return _token()

    async def verify(issued, **_):
        raise GitHubKeyRefused("card_account_mismatch", "The key acts as someone-else.")

    calls: list[list[str]] = []
    monkeypatch.setattr(cli, "_GitHubKeySession", Session)
    monkeypatch.setattr(cli, "verified_login", verify)
    monkeypatch.setattr(cli, "find_gh", lambda **_: "/opt/gh")
    monkeypatch.setattr(cli.subprocess, "call", lambda argv, env=None: calls.append(list(argv)) or 0)

    assert cli._gh_command(SimpleNamespace(gh_args=["--", "-R", REPO, "pr", "list"])) == 1  # noqa: SLF001
    assert calls == []
    err = capsys.readouterr().err
    assert "card_account_mismatch" in err and "ghu_never_printed" not in err


def test_a_plain_push_from_a_card_managed_clone_is_checked_too(tmp_path, monkeypatch, capsys):
    """Root, 2026-10-12: "plain push must not remain an unchecked bypass"."""

    clone = _stamped(tmp_path)
    (clone / "d.txt").write_text("d\n", encoding="utf-8")
    _git("add", "d.txt", cwd=clone)
    _git("-c", "user.email=personal@example.test", "commit", "-q", "-m", "d", cwd=clone)
    pushed: list[list[str]] = []
    real_call = subprocess.call
    monkeypatch.setattr(cli.subprocess, "call",
                        lambda argv, *a, **k: pushed.append(list(argv)) or 0 if argv[:2] == ["git", "push"]
                        else real_call(argv, *a, **k))
    _refusal(clone, monkeypatch)  # installs the session and account fakes

    assert cli._push_command(SimpleNamespace(git_args=["--", "origin", "HEAD:refs/heads/w"])) == 2  # noqa: SLF001
    assert pushed == []
    assert "push_commit_identity_mismatch" in capsys.readouterr().err

    _git("config", "remote.origin.pushurl", "git@github.com:example-org/applications.git", cwd=clone)
    assert cli._push_command(SimpleNamespace(git_args=["--", "origin", "main"])) == 2  # noqa: SLF001
    assert "pushes over HTTPS only" in capsys.readouterr().err and pushed == []


def test_a_clone_outside_any_card_route_is_refused_not_pushed_with_the_machines_key(tmp_path, monkeypatch, capsys):
    """W942 (operator, 2026-10-23): "only github defined in operator card should be used", "this is prohibited
    to use on-host github". An SSH-only clone used to plain-push with this machine's deploy key."""
    clone = _clone(tmp_path, origin="git@github.com:example-org/deploy-only.git", helper="")
    pushed: list[list[str]] = []
    monkeypatch.setattr(cli.subprocess, "call", lambda argv, *a, **k: pushed.append(list(argv)) or 0)
    monkeypatch.chdir(clone)
    assert cli._push_command(SimpleNamespace(git_args=["--", "origin", "main"])) == 2  # noqa: SLF001
    assert pushed == []
    err = capsys.readouterr().err
    assert "push_remote_not_owner_key" in err and "pb worker connect-project" in err


def test_an_https_clone_without_the_card_account_is_refused(tmp_path, monkeypatch, capsys):
    """An HTTPS origin answered by the host's own GitHub login is not the owner's key either (W942)."""
    clone = _clone(tmp_path, origin=f"https://github.com/{REPO}.git", helper="!gh auth git-credential")
    pushed: list[list[str]] = []
    monkeypatch.setattr(cli.subprocess, "call", lambda argv, *a, **k: pushed.append(list(argv)) or 0)
    monkeypatch.chdir(clone)
    assert cli._push_command(SimpleNamespace(git_args=["--", "origin", "main"])) == 2  # noqa: SLF001
    assert pushed == []
    assert "clone_unstamped" in capsys.readouterr().err
