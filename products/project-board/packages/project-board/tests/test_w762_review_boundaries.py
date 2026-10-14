"""W762 boundaries from Root's review of 2b4a948b (2026-10-12), kept as regression tests.

Reviewer probes: local fixtures only; no real credentials or network."""
import subprocess
from types import SimpleNamespace

import pytest
from project_board.client import cli
from project_board.client.github_key import GitHubToken

PROJECT = "work:project:review-fixture"
REPO = "example-org/review-fixture"
EXPECTED_HELPER = "!pb worker git-credential --runtime-kind claude-code --runtime-session-id current-session"
OTHER_HELPER = "!pb worker git-credential --runtime-kind claude-code --runtime-session-id another-session"


def git(folder, *args):
    return subprocess.run(["git", "-C", str(folder), *args], capture_output=True,
                          text=True, check=True).stdout.strip()


@pytest.fixture
def clone(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    folder = tmp_path / "repo"
    folder.mkdir()
    git(folder, "init", "-q", "-b", "main")
    git(folder, "remote", "add", "origin", f"https://github.com/{REPO}.git")
    for key, value in (("user.name", "review fixture"), ("user.email", "card@example.invalid"),
                       ("pb.githubAccount", "card-account"), ("pb.projectRef", PROJECT)):
        git(folder, "config", "--local", key, value)
    git(folder, "config", "--local", "credential.https://github.com.helper", "")
    git(folder, "config", "--local", "--add", "credential.https://github.com.helper", EXPECTED_HELPER)
    git(folder, "commit", "--allow-empty", "-q", "-m", "fixture baseline")
    git(folder, "update-ref", "refs/remotes/origin/main", "HEAD")
    return folder


def token():
    return GitHubToken(token="fixture-not-a-real-key", expires_at=0,
                       login="card-account", commit_email="card@example.invalid", repository=REPO)


def check(clone, monkeypatch, push_args):
    class Session:
        def __init__(self, args):
            self.identity = SimpleNamespace(worker_name="current-session")
            self.project_ref = PROJECT

        def token(self, repository):
            return token()

        def helper(self):
            return EXPECTED_HELPER

    async def verify(issued):
        return issued.login

    monkeypatch.setattr(cli, "_GitHubKeySession", Session)
    monkeypatch.setattr(cli, "verified_login", verify)
    monkeypatch.chdir(clone)

    def git_out(*args):
        result = subprocess.run(["git", *args], capture_output=True, text=True, check=False)
        return result.stdout.strip() if result.returncode == 0 else ""

    args = SimpleNamespace(runtime_kind="claude-code", runtime_session_id="current-session")
    return cli._card_push_refusal(args, "origin", f"https://github.com/{REPO}.git", push_args, git_out)


def test_push_refuses_another_native_sessions_helper(clone, monkeypatch):
    git(clone, "config", "--local", "--unset-all", "credential.https://github.com.helper")
    git(clone, "config", "--local", "credential.https://github.com.helper", "")
    git(clone, "config", "--local", "--add", "credential.https://github.com.helper", OTHER_HELPER)
    refused = check(clone, monkeypatch, ["origin", "HEAD:refs/heads/main"])
    assert "clone_credential_override" in refused, "A prefix-only helper check accepted another native session"


def test_push_all_checks_wrong_email_on_a_non_head_branch(clone, monkeypatch):
    git(clone, "checkout", "-q", "-b", "side")
    git(clone, "-c", "user.email=other@example.invalid", "commit", "--allow-empty", "-q", "-m", "side fixture")
    git(clone, "checkout", "-q", "main")
    refused = check(clone, monkeypatch, ["origin", "--all"])
    assert "push_commit_identity_mismatch" in refused, "--all checked only HEAD and missed the side branch"


def test_refresh_does_not_rewrite_another_workers_same_project_clone(clone):
    git(clone, "config", "--local", "--unset-all", "credential.https://github.com.helper")
    git(clone, "config", "--local", "credential.https://github.com.helper", OTHER_HELPER)
    git(clone, "config", "--local", "user.email", "other@example.invalid")
    git(clone, "config", "--local", "pb.githubAccount", "other-account")
    cli._refresh_clone_stamp(PROJECT, token(), str(clone), helper=EXPECTED_HELPER)
    assert git(clone, "config", "--local", "--get", "user.email") == "other@example.invalid", \
        "Project/repository matching alone rewrote another worker/inviter's clone"
