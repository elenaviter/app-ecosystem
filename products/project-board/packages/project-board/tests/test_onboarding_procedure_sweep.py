"""Every W304 onboarding finding is written where a host agent or a person meets it.

The operator, 2026-09-24: "be sure this all documented in procedure."
claude-ops re-onboarded on spark1 from the procedure alone, and each gap it
hit was a step the procedure did not name. These pins keep the steps named.
"""

from __future__ import annotations

import re
from pathlib import Path

from project_board.client.procedures import source_package_path

PROCEDURES = source_package_path().parent
REPO = Path(__file__).resolve().parents[5]


def _words(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


HOST = _words(PROCEDURES / "add-a-worker-host.md")
GUIDE = _words(REPO / "products" / "project-board" / "docs" / "add-a-machine.md")


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
    # Operator ruling 2026-09-26 (W304 decision 3): "*" is the default; old hosts may be empty.
    words = " ".join(HOST.split())
    assert "`pb setup` gives a new host the default teammate list `*`" in words
    assert "may still hold an empty list and refuse every teammate's mail" in words
    assert "pb host configure --allow-peer-worker '*'" in HOST and "pb host configure --deny-all-peers" in HOST
    assert "Who may write to your agents" in GUIDE
    assert "A new machine starts with everyone on your projects (`*`)" in GUIDE


def test_37_joining_sends_the_welcome_notice_and_the_agent_reports_ready():
    # W304 finding 37 is fixed (applications#101): step 12 describes the notice
    # and the ready message it asks for, no longer a pending gap.
    words = " ".join(HOST.split())
    assert "no welcome message yet" not in words
    assert "project sends the agent one welcome notice" in words
    assert "tell the coordinator it is ready, naming anything it could not reach" in words


def test_16_device_login_is_proven_live_and_the_tunnel_is_the_fallback():
    words = " ".join(HOST.split())
    assert "Known gap: device login is not yet proven live" not in words
    assert "Device login is proven live end to end" in words
    assert "If it fails, the fallback is the callback through an SSH tunnel." in words


def test_u4_agent_workspaces_live_under_kdcube():
    # Operator ruling, 2026-09-25: "i do not want this in user folder."
    # Every spelling of the old home-folder root: ~/, $HOME/ (escaped in tmux), /home/<user>/.
    # The move section names the old folders on purpose; nothing else may.
    outside_move = HOST.split("### Move an existing agent to the workspace root", 1)[0] + HOST.split("## 10. Enroll each agent", 1)[1]
    assert re.findall(r"(?:~|\$HOME|/home/<user>)/workspaces", outside_move) == []
    assert "--allow-root /home/<user>/.kdcube/pb/workspaces" in HOST
    assert 'W="$HOME/.kdcube/pb/workspaces/<alias>"' in HOST
    assert "mkdir -p ~/.kdcube/pb/workspaces && chmod 700 ~/.kdcube/pb/workspaces" in HOST
    assert 'exec claude ${1:+--resume "$1"} --add-dir "$W"' in HOST
    assert "An existing host keeps its old folders until a planned move." in HOST
    collaboration = _words(PROCEDURES / "problem-board-worker" / "references" / "collaboration.md")
    assert "worktree add --detach ~/.kdcube/pb/workspaces/<alias>/<repo> origin/main" in collaboration
    assert "`~/.kdcube/pb/workspaces/<alias>/<repo>`" in collaboration


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
    for number in (*range(0, 2), "2 to 3", *range(4, 7), "7, at 12", *range(8, 15)):
        assert f"| {number} |" in table, number
    for owner in ("**operator**", "either", "**machine administrator**", "host agent"):
        assert owner in table, owner
    # Step 4 runs sudo loginctl enable-linger: it is the machine administrator's.
    assert "| 4 | **machine administrator** |" in table
    guide = GUIDE[GUIDE.index("## Who does what"):GUIDE.index("## What you need first")]
    for number in ("| 0.", "| 1.", "| 2 to 3.", "| 4.", "| 5.", "| 6.", "| 7.", "| 8.", "| 9.", "| 10.", "| 11.", "| 12.", "| 13.", "| 14."):
        assert number in guide, number
    assert "| 4. Keep the agents running after logout | **The machine's administrator** |" in guide


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
        "OK\nproject_on_this_host = True\nrepositories[0].alias = applications\nrepositories[0].url = git@github.com:kdcube/applications.git\n"
        "repositories[1].alias = reachable\nrepositories[1].url = git@github.com:owner/reachable.git\n"
        "repositories[2].alias = clash\nrepositories[2].url = git@github.com:owner/clash-a.git\n",
        encoding="utf-8",
    )
    (tmp_path / "beta").write_text(
        "OK\nproject_on_this_host = True\nrepositories[0].alias = kdcube\nrepositories[0].url = https://github.com/kdcube/kdcube\n"
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
    # W346: macOS's realpath refuses GNU's -m, and the second run then
    # reported every existing key block as a CONFLICT on the macOS host only.
    # The script needs no realpath at all, so this one refuses every call, on
    # every platform.
    (bin_dir / "realpath").write_text(
        "#!/bin/sh\necho \"realpath is not portable: $*\" >&2\nexit 64\n", encoding="utf-8"
    )
    for tool in ("pb", "git", "realpath"):
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
    # The block the first run wrote is recognised as this key's, not a conflict.
    assert "CONFLICT github-applications" not in second and "CONFLICT github-betaonly" not in second
    assert "ok reachable" in second
    assert "realpath is not portable" not in first + second



def _stopped_run(tmp_path, pb_script: str) -> tuple[int, str]:
    import subprocess

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    keys = tmp_path / "keys"
    keys.mkdir(exist_ok=True, mode=0o700)
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "host deploy key: needed owner/needed",
                    "-f", str(keys / "deploy_needed")], check=True)
    (bin_dir / "pb").write_text(pb_script, encoding="utf-8")
    (bin_dir / "pb").chmod(0o755)
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "HOME": str(tmp_path),
           "KEYS": str(keys), "SSH_CONFIG": str(keys / "config"), "HOST_ID": "host"}
    done = subprocess.run(["bash", "-c", _step7_script()], env=env, capture_output=True, text=True, timeout=60)
    return done.returncode, done.stdout + done.stderr


def test_15_step_7_stops_before_any_grant_or_revoke_when_collection_fails(tmp_path):
    # ae#120 review (codex-ui): without fail-closed collection a failed list,
    # or one failed context of two, left an empty or partial card and the
    # script revoked keys valid projects still needed.
    import shutil

    import pytest

    if not (shutil.which("ssh-keygen") and shutil.which("bash")):
        pytest.skip("needs ssh-keygen and bash")
    (tmp_path / "list").write_text(
        "OK\n--- a@host (claude-code-aaa)\nruntime claude-code · pool active\nattends: work:project:alpha\n"
        "--- b@host (codex-bbb)\nruntime codex · pool active\nattends: work:project:beta\n",
        encoding="utf-8",
    )
    (tmp_path / "card").write_text(
        "OK\nproject_on_this_host = True\nrepositories[0].alias = needed\nrepositories[0].url = git@github.com:owner/needed.git\n",
        encoding="utf-8",
    )
    (tmp_path / "absent").write_text("OK\nproject_on_this_host = False\n", encoding="utf-8")
    two = f"case \"$2\" in list) cat {tmp_path / 'list'};; context) case \"$*\" in *work:project:alpha*) cat {tmp_path / 'card'};; *) "
    cases = {
        "list fails": "#!/bin/sh\n[ \"$2\" = list ] && exit 1\nexit 0\n",
        "one context of two fails": "#!/bin/sh\n" + two + "exit 3;; esac;; esac\n",
        "a project not on the host yet": "#!/bin/sh\n" + two + f"cat {tmp_path / 'absent'};; esac;; esac\n",
    }
    for index, (name, script) in enumerate(cases.items()):
        case_dir = tmp_path / f"case{index}"
        case_dir.mkdir()
        code, output = _stopped_run(case_dir, script)
        assert code != 0, name
        assert "STOP:" in output and "Nothing granted or revoked." in output, (name, output)
        assert "REVOKE" not in output and "GRANT" not in output, (name, output)
def test_15_step_7_retires_a_moved_alias_and_reads_host_blocks_through_ssh(tmp_path):
    # ae#120 review (codex-ui): an alias retargeted to another repository left
    # the old repository's key authorized, and an exact-line match missed a
    # valid Host block written with several names or another case.
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
        "OK\n--- a@host (claude-code-aaa)\nruntime claude-code · pool active\nattends: work:project:alpha\n",
        encoding="utf-8",
    )
    (tmp_path / "alpha").write_text(
        "OK\nproject_on_this_host = True\n"
        "repositories[0].alias = moved\nrepositories[0].url = git@github.com:owner/new-home.git\n"
        "repositories[1].alias = multi\nrepositories[1].url = git@github.com:owner/multi.git\n"
        "repositories[2].alias = bad\nrepositories[2].url = git@github.com:owner/bad.git\n",
        encoding="utf-8",
    )
    (bin_dir / "pb").write_text(
        f"#!/bin/sh\ncase \"$2\" in list) cat {tmp_path / 'list'};; context) cat {tmp_path / 'alpha'};; esac\n",
        encoding="utf-8",
    )
    (bin_dir / "git").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    for tool in ("pb", "git"):
        (bin_dir / tool).chmod(0o755)
    for name, comment in (("deploy_moved", "host deploy key: moved owner/old-home"), ("deploy_multi", "host deploy key: multi owner/multi")):
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", comment, "-f", str(keys / name)], check=True)
    old_fingerprint = subprocess.run(
        ["ssh-keygen", "-lf", str(keys / "deploy_moved.pub")], capture_output=True, text=True, check=True
    ).stdout.split()[1]
    (keys / "config").write_text(
        f"host github-multi github-other\n    hostname github.com\n    identityfile {keys / 'deploy_multi'}\n\n"
        "Host github-b*\n  HostName gitlab.com\n",
        encoding="utf-8",
    )
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
        "HOME": str(tmp_path), "KEYS": str(keys), "SSH_CONFIG": str(keys / "config"), "HOST_ID": "host",
    }

    def run() -> str:
        done = subprocess.run(["bash", "-c", _step7_script()], env=env, capture_output=True, text=True, timeout=60)
        return done.stdout + done.stderr

    first = run()
    # The moved alias: the old pair is retired and revoked on the old repository,
    # and a new pair is granted on the new one.
    assert "### REVOKE moved (the card moved this alias to another repository)" in first
    assert "Repository: owner/old-home" in first and old_fingerprint in first
    assert "### GRANT moved" in first and "https://github.com/owner/new-home/settings/keys" in first
    assert list(keys.glob("retired_moved_*.pub"))
    new_fingerprint = subprocess.run(
        ["ssh-keygen", "-lf", str(keys / "deploy_moved.pub")], capture_output=True, text=True, check=True
    ).stdout.split()[1]
    assert new_fingerprint != old_fingerprint
    # A lower-case, several-name block is ours: nothing is appended for it.
    config = (keys / "config").read_text(encoding="utf-8")
    assert "Host github-multi\n" not in config
    # A pattern block that sends the alias elsewhere is refused.
    assert "CONFLICT github-bad" in first and "resolves to gitlab.com" in first
    # The retired key keeps being named until its files are removed.
    assert "### REVOKE moved (the card moved this alias to another repository)" in run()


def test_u5_gh_is_signed_in_per_user_with_the_operators_chosen_identity():
    # W304 U5 (2026-09-25): agents open their own PRs and post verdicts with gh.
    words = " ".join(HOST.split())
    assert "| **GitHub identity for pull requests** |" in HOST
    assert "### GitHub CLI for pull requests and review verdicts" in HOST
    assert "read -rs T && printf '%s\\n' \"$T\" | gh auth login --with-token; unset T" in HOST
    assert "never in a file or an environment variable" in words
    assert "| 7 | **operator** | create a token for the GitHub identity chosen in step 0" in HOST


def test_12_codex_has_a_complete_headless_login_and_start_contract():
    assert "`~/.local/node/bin/codex`" in HOST
    assert "service environment" in HOST
    assert "`codex login --device-auth`" in HOST
    assert "`ssh -L 1455:localhost:1455 -i <key> <user>@<host>`" in HOST
    assert "`codex login status`" in HOST
    assert "--sandbox danger-full-access --ask-for-approval never --search" in HOST
    assert '--add-dir "$HOME/.kdcube"' in HOST
    assert "`on-request` is the attended diagnostic mode" in HOST
    assert "`codex queue --thread <session-id>`" in HOST
    assert 'exec codex ${1:+resume "$1"} -C "$W"' in HOST

    assert "`codex login --device-auth`" in GUIDE
    assert "`~/.local/node/bin/codex`" in GUIDE
    assert "SSH tunnel for Codex browser login" in GUIDE
    assert "Codex uses `--ask-for-approval never`" in GUIDE


def test_35_each_agent_starts_and_resumes_from_one_script_with_every_flag(tmp_path):
    # W304 finding 35 (2026-09-24): a resume command pasted by hand was cut and
    # both spark1 agents lost three flags. The scripts are run here, as written.
    import subprocess

    scripts = re.findall(r"cat > ~/.local/bin/start-<agent-name> <<'EOF'\n(.*?)\nEOF\n",
                         (PROCEDURES / "add-a-worker-host.md").read_text(encoding="utf-8"), re.S)
    assert len(scripts) == 2, "one start script for Claude Code, one for Codex"
    home = tmp_path / "home"
    bin_dir = home / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    workspace = home / ".kdcube" / "pb" / "workspaces" / "al"
    workspace.mkdir(parents=True)
    for runtime in ("claude", "codex"):
        stub = bin_dir / runtime
        stub.write_text(f'#!/bin/sh\necho {runtime} "$@"\n')
        stub.chmod(0o755)
    runs = {}
    for index, body in enumerate(scripts):
        script = tmp_path / f"start{index}"
        script.write_text(body.replace("<alias>", "al").replace("<agent-name>", "ag") + "\n")
        for args in ((), ("sid-1",)):
            done = subprocess.run(["bash", str(script), *args], env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
                                  capture_output=True, text=True, check=True)
            runs[(index, args)] = done.stdout.split()
    home_s, ws = str(home), str(workspace)
    claude_flags = ["--add-dir", ws, "--add-dir", f"{home_s}/.kdcube", "--dangerously-skip-permissions",
                    "--disallowedTools", "AskUserQuestion"]
    assert runs[(0, ())] == ["claude", *claude_flags]
    assert runs[(0, ("sid-1",))] == ["claude", "--resume", "sid-1", *claude_flags]
    codex_flags = ["-C", ws, "--sandbox", "danger-full-access", "--ask-for-approval", "never", "--search",
                   "--add-dir", f"{home_s}/.kdcube"]
    assert runs[(1, ())] == ["codex", *codex_flags]
    assert runs[(1, ("sid-1",))] == ["codex", "resume", "sid-1", *codex_flags]
    words = " ".join(HOST.split())
    assert "\"bash -lc 'start-<agent-name>; exec bash'\"" in HOST
    assert "`start-<agent-name> <session-id>`" in HOST
    assert "The session id is the agent's stable name without its `claude-code-` or `codex-` prefix." in words
    assert "**Restart an agent**" in HOST and "Resume as <agent-name>: Start Or Resume." in HOST
    assert "Agents that must not reach each other run as separate host users." in words


def test_u4_move_keeps_conversation_memory_and_worktrees_and_rolls_back(tmp_path):
    # Operator ruling 2026-09-26 (W304 decision 4): existing agents move to the
    # new root, conversations included. The scripts are run here as written.
    import subprocess

    raw = (PROCEDURES / "add-a-worker-host.md").read_text(encoding="utf-8")
    section = raw.split("### Move an existing agent to the workspace root", 1)[1].split("## 10. Enroll each agent", 1)[0]
    move, rollback = re.findall(r"```bash\n(.*?)\n```", section, re.S)
    assert "mv \"$PO\" \"$PN\"" in move and move.index('mv "$PO" "$PN"') < move.index('mv "$OLD" "$NEW"')
    home = tmp_path / "home"
    old = home / "workspaces" / "space009"
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_NOSYSTEM": "1"}
    run = lambda *args, **kw: subprocess.run(args, env=env, check=True, capture_output=True, text=True, **kw)
    shared = home / "src" / "repo"
    run("git", "init", "-q", str(shared))
    run("git", "-C", str(shared), "commit", "-q", "--allow-empty", "-m", "init")
    run("git", "-C", str(shared), "worktree", "add", "-q", str(old / "repo"), "-b", "a")
    run("git", "clone", "-q", str(shared), str(old / "clone"))
    run("git", "-C", str(old / "clone"), "worktree", "add", "-q", str(old / "worktrees" / "w2"), "-b", "x")
    # Claude Code's encoding: every character not a letter or digit becomes "-".
    enc = lambda path: re.sub(r"[^A-Za-z0-9]", "-", str(path))
    projects = home / ".claude" / "projects"
    (projects / enc(old) / "memory").mkdir(parents=True)
    (projects / enc(old) / "sid-1.jsonl").write_text("{}\n")
    (projects / enc(old) / "memory" / "MEMORY.md").write_text("- a fact\n")
    values = f"A=ag ALIAS=al@spark1 SID=sid-1\nOLD={old}\n"
    fill = lambda script: re.sub(r"A=<agent-name> ALIAS=<alias> SID=<session-id>\nOLD=<[^\n]*>\n", values, script)
    run("bash", "-c", fill(move))
    new = home / ".kdcube" / "pb" / "workspaces" / "al@spark1"
    assert not old.exists() and not (projects / enc(old)).exists()
    assert (projects / enc(new) / "memory" / "MEMORY.md").read_text() == "- a fact\n"
    assert (projects / enc(new) / "sid-1.jsonl").exists()
    for tree, branch in (("repo", "a"), ("worktrees/w2", "x")):
        assert run("git", "-C", str(new / tree), "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == branch
    for checkout in (shared, new / "clone"):
        listing = run("git", "-C", str(checkout), "worktree", "list").stdout
        assert str(old) not in listing and "prunable" not in listing
    # A second run refuses: the new place is taken.
    assert subprocess.run(["bash", "-c", fill(move)], env=env, capture_output=True, text=True).returncode != 0

    run("bash", "-c", fill(rollback).replace("# same A, ALIAS, SID, OLD, NEW, enc, PO, PN as above",
                                             fill(move).split("[ -d \"$OLD\" ]", 1)[0].split("( set -eu", 1)[1]))
    assert old.exists() and not new.exists()
    assert (projects / enc(old) / "memory" / "MEMORY.md").exists() and not (projects / enc(new)).exists()
    assert run("git", "-C", str(old / "repo"), "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "a"
    assert str(new) not in run("git", "-C", str(shared), "worktree", "list").stdout
