"""Every W304 onboarding finding is written where a host agent or a person meets it.

The operator, 2026-09-24: "be sure this all documented in procedure."
claude-ops re-onboarded on spark1 from the procedure alone, and each gap it
hit was a step the procedure did not name. These pins keep the steps named.
"""

from __future__ import annotations

from pathlib import Path

from project_board.client.procedures import source_package_path

PROCEDURES = source_package_path().parent
REPO = Path(__file__).resolve().parents[5]


def _words(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


HOST = _words(PROCEDURES / "add-a-worker-host.md")
GUIDE = _words(REPO / "docs" / "project-board" / "add-a-machine.md")


def test_45_the_install_merges_the_claude_code_settings_and_says_how():
    assert "`pb procedure install --target claude-code` (step 3) merges them in" in HOST
    assert "`settings.json.bak-<UTC time>`, readable by the user only, and never replaces an earlier backup" in HOST
    assert "the undo command (copy the backup back)" in HOST
    assert "A status line that already runs something else is left as it is and named in `notes`" in HOST
    # codex-main's review of #116: older entries updated, the pb that ran the install, a symlink kept.
    assert "a bare `pb` becomes the full path" in HOST
    assert "a `StopFailure` matcher that covered only `rate_limit` gains every other stopping error" in HOST
    assert "an install that cannot name its `pb` is refused before it writes anything" in HOST
    assert "A `settings.json` that is a symlink stays one, and the file it points to is what changes" in HOST
    assert "9. Usage and watch settings" in GUIDE
    assert "Installing the procedure sets this up in the machine's Claude Code settings, keeps a copy of the previous settings" in GUIDE


def test_24_an_approved_agent_is_told_to_follow_start_or_resume():
    assert "Authorized. Follow Start Or Resume of the problem-board-worker skill." in HOST
    assert "11. Tell each agent it is approved" in GUIDE


def test_35_sessions_restart_in_place_after_an_update():
    assert "Restart the agent sessions after an update" in HOST
    assert "`/exit`, then the full resume command from step 9" in HOST
    assert "After each update" in GUIDE


def test_38_44_the_peer_list_is_set_on_every_host_old_ones_included():
    assert "pb host configure --allow-peer-worker" in HOST
    assert "A host set up before this setting existed has an empty list" in HOST
    assert "Who may write to your agents" in GUIDE


def test_37_the_welcome_on_joining_is_named_as_pending():
    assert "Joining a project sends the agent no welcome message yet (W304 finding 37, pending)" in HOST


def test_25_33_43_the_watch_and_its_guard_are_the_agent_s_own():
    assert "keeps its own inbox watch and the guard prompt that renews it" in HOST


def test_15_repositories_and_deploy_keys_come_from_the_project_card():
    # W304 finding 15 (operator direction under finding 14): the repositories
    # are a project property, so step 0 no longer asks for them, and a host
    # gains keys and clones for exactly the project card's list.
    step0 = HOST[HOST.index("## 0. Decide before starting"):HOST.index("## 1. Give the host agent")]
    assert "repositories the agents may work on" not in step0
    assert "the project the agents join" in step0
    assert "Repositories are not a host decision." in step0
    assert "repositories.tsv" not in HOST
    step7 = HOST[HOST.index("## 7. "):HOST.index("## 8. ")]
    assert "**The project card decides, in both directions**" in step7
    assert "named by the card's `alias`" in step7
    assert "a **revoke** block for each key this procedure made whose alias is on no attended project's card" in step7
    step8 = HOST[HOST.index("## 8. "):HOST.index("## 9. ")]
    assert "git clone" not in step8
    step12 = HOST[HOST.index("## 12. "):HOST.index("## 13. ")]
    assert "The host agent runs step 7's reconciliation on this host" in step12
    assert "Repositories the agents may work in" not in GUIDE
    assert "Project the agents join: <project name>" in GUIDE
    assert "you make it on the project card, not per machine" in GUIDE


def test_every_step_has_an_owner_in_both_tables():
    # W304 "Who does what": both documents name an owner for every step.
    table = HOST[HOST.index("## Who does what"):HOST.index("## What you end up with")]
    for number in (*range(0, 2), "2 to 4", *range(5, 7), "7, at 12", *range(8, 15)):
        assert f"| {number} |" in table, number
    for owner in ("**operator**", "either", "**machine administrator**", "host agent"):
        assert owner in table, owner
    guide = GUIDE[GUIDE.index("## Who does what"):GUIDE.index("## What you need first")]
    for number in ("| 0.", "| 1.", "| 5.", "| 6.", "| 7.", "| 8.", "| 9.", "| 10.", "| 11.", "| 12."):
        assert number in guide, number


def _step7_script() -> str:
    text = (PROCEDURES / "add-a-worker-host.md").read_text(encoding="utf-8")
    step7 = text[text.index("## 7. "):text.index("## 8. ")]
    return step7[step7.index("```bash\n") + len("```bash\n"):step7.index("\n```", step7.index("```bash\n"))]


def test_15_step_7_reconciles_keys_with_the_card_in_both_directions(tmp_path):
    # ae#120 reviews (codex-ui): keys were only ever added, a removed
    # repository kept its write key, an existing key skipped a lost SSH block
    # forever, and reconciling one project revoked a key another attended
    # project still needed (keys are shared by every agent of the user). The
    # published script is run as written, with a fake pb (two attending
    # agents, two cards) and a fake git (no network).
    import shutil
    import subprocess

    import pytest

    if not (shutil.which("ssh-keygen") and shutil.which("ssh") and shutil.which("bash")):
        pytest.skip("needs ssh-keygen, ssh and bash")
    keys = tmp_path / "keys"
    keys.mkdir(mode=0o700)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (tmp_path / "list").write_text(
        "OK\n--- a@host (claude-code-aaa)\nruntime claude-code · pool active\nattends: work:project:alpha\n"
        "--- b@host (codex-bbb)\nruntime codex · pool active\nattends: work:project:beta\n"
        "--- idle@host (claude-code-ccc)\nruntime claude-code · pool active\n",
        encoding="utf-8",
    )
    (tmp_path / "alpha").write_text(
        "OK\nrepositories[0].alias = applications\nrepositories[0].url = git@github.com:kdcube/applications.git\n"
        "repositories[1].alias = reachable\nrepositories[1].url = git@github.com:owner/reachable.git\n"
        "repositories[2].alias = clash\nrepositories[2].url = git@github.com:owner/clash-a.git\n",
        encoding="utf-8",
    )
    (tmp_path / "beta").write_text(
        "OK\nrepositories[0].alias = kdcube\nrepositories[0].url = https://github.com/kdcube/kdcube\n"
        "repositories[1].alias = betaonly\nrepositories[1].url = git@github.com:owner/betaonly.git\n"
        "repositories[2].alias = clash\nrepositories[2].url = git@github.com:owner/clash-b.git\n",
        encoding="utf-8",
    )
    (bin_dir / "pb").write_text(
        "#!/bin/sh\n"
        f"case \"$2\" in list) cat {tmp_path / 'list'};; context) case \"$*\" in "
        f"*work:project:alpha*) cat {tmp_path / 'alpha'};; *work:project:beta*) cat {tmp_path / 'beta'};; esac;; esac\n",
        encoding="utf-8",
    )
    (bin_dir / "git").write_text(
        "#!/bin/sh\n[ \"$1\" = ls-remote ] && case \"$2\" in github-reachable:*) exit 0;; esac\nexit 1\n",
        encoding="utf-8",
    )
    for tool in ("pb", "git"):
        (bin_dir / tool).chmod(0o755)
    keygen = lambda name, comment: subprocess.run(  # noqa: E731
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", comment, "-f", str(keys / name)], check=True
    )
    keygen("deploy_removed", "host deploy key: removed owner/removed")  # on no attended card
    keygen("deploy_betaonly", "host deploy key: betaonly owner/betaonly")  # only project beta needs it
    keygen("deploy_applications", "old")  # the SSH block was lost
    (keys / "deploy_applications.pub").unlink()  # and so was the public half
    (keys / "config").write_text("Host github-kdcube\n  HostName example.com\n  IdentityFile /elsewhere\n", encoding="utf-8")
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
        "HOME": str(tmp_path), "KEYS": str(keys), "SSH_CONFIG": str(keys / "config"), "HOST_ID": "host",
    }

    def run() -> str:
        done = subprocess.run(["bash", "-c", _step7_script()], env=env, capture_output=True, text=True, timeout=60)
        return done.stdout + done.stderr

    first = run()
    assert "### GRANT applications" in first and "Page: https://github.com/kdcube/applications/settings/keys" in first
    assert "ok reachable" in first
    assert "CONFLICT github-kdcube" in first and "Left unchanged." in first
    assert "CONFLICT alias clash names different repositories" in first
    assert "### REVOKE removed (on no attended project's card)" in first and "Repository: owner/removed" in first
    # A key only the other attended project needs is kept.
    assert "REVOKE betaonly" not in first and "### GRANT betaonly" in first
    config = (keys / "config").read_text(encoding="utf-8")
    assert config.count("Host github-applications") == 1 and "HostName example.com" in config
    assert (keys / "deploy_applications.pub").read_text(encoding="utf-8").endswith("host deploy key: applications kdcube/applications\n")
    second = run()
    assert (keys / "config").read_text(encoding="utf-8").count("Host github-applications") == 1
    assert "### GRANT applications" in second and "### REVOKE removed" in second and "REVOKE betaonly" not in second
