"""W762 matching refspecs from Root's re-review of 02b6bb78 (2026-10-12), kept as regression tests.

Offline matching-refspec probes for exact AE 02b6bb78; fake identities only."""

import subprocess

import pytest

from test_w762_review_boundaries import check, clone, git  # noqa: F401


@pytest.mark.parametrize("configured", [False, True], ids=["explicit-colon", "configured-colon"])
@pytest.mark.parametrize("refspec", [":", "+:"], ids=["matching", "force-matching"])
def test_matching_colon_refspec_checks_side_branch(clone, monkeypatch, tmp_path, configured, refspec):
    baseline = git(clone, "rev-parse", "HEAD")
    remote = tmp_path / "offline-matching.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], capture_output=True, check=True)
    # Populate a temporary local fixture, not a user's repository or external remote.
    subprocess.run(["git", "--git-dir", str(remote), "fetch", "-q", str(clone),
                    f"{baseline}:refs/heads/main"], capture_output=True, check=True)
    subprocess.run(["git", "--git-dir", str(remote), "update-ref", "refs/heads/side", baseline],
                   capture_output=True, check=True)
    git(clone, "remote", "set-url", "origin", str(remote))
    git(clone, "checkout", "-q", "-b", "side")
    git(clone, "-c", "user.email=other@example.invalid", "commit", "--allow-empty", "-q", "-m", "side fixture")
    git(clone, "checkout", "-q", "main")
    if configured:
        git(clone, "config", "remote.origin.push", refspec)
    push_args = ["origin"] if configured else ["origin", refspec]
    refused = check(clone, monkeypatch, push_args)
    dry_run = subprocess.run(["git", "-C", str(clone), "push", "--dry-run", "--porcelain", *push_args],
                             capture_output=True, text=True, check=True)
    assert "refs/heads/side:refs/heads/side" in dry_run.stdout
    assert "push_commit_identity_mismatch" in refused, \
        "The special ':' matching refspec sends the side commit; the resolver treats it as deletion and checks HEAD"
