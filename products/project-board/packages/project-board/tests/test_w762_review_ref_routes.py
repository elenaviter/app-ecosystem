"""W762 ref routes from Root's re-review of 1ee2b202 (2026-10-12), kept as regression tests.

Offline review probes for exact AE 1ee2b202; no real credentials or remote writes."""

import subprocess

from test_w762_review_boundaries import check, clone, git  # noqa: F401


def side_with_wrong_email(folder):
    git(folder, "checkout", "-q", "-b", "side")
    git(folder, "-c", "user.email=other@example.invalid", "commit", "--allow-empty", "-q", "-m", "side fixture")
    git(folder, "checkout", "-q", "main")


def assert_git_would_send_side(folder, tmp_path, *push_args):
    remote = tmp_path / "offline-remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True, capture_output=True)
    git(folder, "remote", "set-url", "origin", str(remote))
    # Local --dry-run only: no commits, refs or credentials are published.
    result = subprocess.run(["git", "-C", str(folder), "push", "--dry-run", "--porcelain", *push_args],
                            capture_output=True, text=True, check=True)
    assert "refs/heads/side:refs/heads/side" in result.stdout


def test_configured_remote_refspec_is_checked(clone, monkeypatch, tmp_path):
    side_with_wrong_email(clone)
    git(clone, "config", "remote.origin.push", "side:refs/heads/side")
    refused = check(clone, monkeypatch, ["origin"])
    assert_git_would_send_side(clone, tmp_path, "origin")
    assert "push_commit_identity_mismatch" in refused, "Configured remote.origin.push sends side, but the guard checks HEAD"


def test_wildcard_refspec_is_expanded_or_refused(clone, monkeypatch, tmp_path):
    side_with_wrong_email(clone)
    refspec = "refs/heads/*:refs/heads/*"
    refused = check(clone, monkeypatch, ["origin", refspec])
    assert_git_would_send_side(clone, tmp_path, "origin", refspec)
    assert refused, "Git accepts the wildcard; git log cannot resolve it, and the guard silently allows the push"


def test_generic_http_authorization_header_is_refused(clone, monkeypatch):
    git(clone, "config", "http.extraHeader", "AUTHORIZATION: basic fixture-not-a-real-credential")
    refused = check(clone, monkeypatch, ["origin", "HEAD:refs/heads/main"])
    assert "clone_credential_override" in refused, "The credential override regex misses the generic http.extraHeader"


def test_push_default_matching_checks_every_branch(clone, monkeypatch):
    side_with_wrong_email(clone)
    git(clone, "config", "push.default", "matching")
    assert "push_commit_identity_mismatch" in check(clone, monkeypatch, ["origin"])


def test_negative_and_bare_wildcard_refspecs_are_refused_by_name(clone, monkeypatch):
    assert "push_selection_unsupported" in check(clone, monkeypatch, ["origin", "^refs/heads/side"])
    assert "push_selection_unsupported" in check(clone, monkeypatch, ["origin", "feature/*:feature/*"])


def test_an_unreadable_selection_is_refused_not_read_as_no_commits(clone, monkeypatch):
    assert "push_selection_unverified" in check(clone, monkeypatch, ["origin", "no-such-branch:refs/heads/x"])
