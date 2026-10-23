"""Every W304 onboarding finding is written where a host agent or a person meets it.

The operator, 2026-09-24: "be sure this all documented in procedure."
claude-ops re-onboarded on spark1 from the procedure alone, and each gap it
hit was a step the procedure did not name. These pins keep the steps named.
"""

from __future__ import annotations

import re
from pathlib import Path

from project_board.client.procedures import source_package_path
from procedure_reference import reference_text

PROCEDURES = source_package_path().parent
REPO = Path(__file__).resolve().parents[5]


def _words(path: Path) -> str:
    return " ".join(reference_text(path).split())


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


def test_16_device_login_is_proven_live_and_has_no_tunnel_fallback():
    # W414, operator 2026-09-30: device login is built in, with no callback,
    # port, tunnel or manual step anywhere; a failure is a named refusal.
    words = " ".join(HOST.split())
    assert "Known gap: device login is not yet proven live" not in words
    assert "Device login is proven live end to end" in words
    assert "there is no callback, port, tunnel or manual step to fall back to" in words
    assert "the fallback is the callback" not in words
    assert "--callback-port 18765" not in words


def test_u4_agent_workspaces_live_under_kdcube():
    # Operator ruling, 2026-09-25: "i do not want this in user folder."
    # Every spelling of the old home-folder root: ~/, $HOME/ (escaped in tmux), /home/<user>/.
    # The move section names the old folders on purpose; nothing else may.
    outside_move = HOST.split("### Move an existing agent to the workspace root", 1)[0] + HOST.split("## 10. Enroll each agent", 1)[1]
    assert re.findall(r"(?:~|\$HOME|/home/<user>)/workspaces", outside_move) == []
    assert "--allow-root /home/<user>/.kdcube/pb/workspaces" in HOST
    assert 'W="$HOME/.kdcube/pb/workspaces/<alias>"' in HOST
    assert "mkdir -p ~/.kdcube/pb/workspaces && chmod 700 ~/.kdcube/pb/workspaces" in HOST
    assert 'exec env CLAUDE_CONFIG_DIR="$HOME/.claude-accounts/<account-email>" claude ${1:+--resume "$1"}' in HOST
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
    # W942 (operator, 2026-10-23): "only github defined in operator card should be used". Step 7 keeps only
    # the revoke sheet for keys made earlier; nothing makes a deploy key.
    assert "**Deploy keys made earlier: the revoke sheet.**" in step7
    assert "**Revoking stays with the script below**" in step7
    assert "makes nothing" in step7 and "ssh-keygen -q -t" not in step7
    step8 = HOST[HOST.index("## 8. "):HOST.index("## 9. ")]
    assert "git clone" not in step8
    step12 = HOST[HOST.index("## 12. "):HOST.index("## 13. ")]
    assert "names it as `needs_key` with the key's answer" in step12 and "no deploy key" in step12
    assert "Repositories the agents may work in" not in GUIDE
    assert "Project the agents join: <project name>" in GUIDE
    assert "you make it on the project card, not per machine" in GUIDE


def test_every_step_has_an_owner_in_both_tables():
    # W304 "Who does what": both documents name an owner for every step.
    table = HOST[HOST.index("## Who does what"):HOST.index("## What you end up with")]
    for number in (*range(0, 2), "2 to 3", *range(4, 15)):
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


def test_15_step_7_only_prints_revoke_blocks_and_makes_nothing(tmp_path):
    """W942: the step 7 script lists every deploy key made earlier for the operator to delete, and makes none."""
    import shutil
    import subprocess

    import pytest

    if not (shutil.which("ssh-keygen") and shutil.which("bash")):
        pytest.skip("needs ssh-keygen and bash")
    keys = tmp_path / "keys"
    keys.mkdir(mode=0o700)
    keygen = lambda name, comment: subprocess.run(  # noqa: E731
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", comment, "-f", str(keys / name)], check=True
    )
    keygen("deploy_applications", "host deploy key: applications kdcube/applications")
    keygen("retired_moved_20261001T000000Z", "host deploy key: moved owner/old-home")
    config = "Host github-applications\n  HostName github.com\n  IdentityFile ~/keys/deploy_applications\n"
    (keys / "config").write_text(config, encoding="utf-8")
    before = sorted(path.name for path in keys.iterdir())
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "HOME": str(tmp_path), "KEYS": str(keys),
           "SSH_CONFIG": str(keys / "config"), "HOST_ID": "host"}

    done = subprocess.run(["bash", "-c", _step7_script()], env=env, capture_output=True, text=True, timeout=60)

    out = done.stdout + done.stderr
    assert done.returncode == 0, out
    assert "### REVOKE applications (deploy keys are not used (W942))" in out
    assert "Repository: kdcube/applications" in out and 'titled "host agents"' in out
    assert '"Host github-applications" block' in out
    assert "### REVOKE moved (a retired key)" in out and "Repository: owner/old-home" in out
    assert "GRANT" not in out
    assert sorted(path.name for path in keys.iterdir()) == before, "nothing is made or removed"
    assert (keys / "config").read_text(encoding="utf-8") == config

    empty = tmp_path / "empty"
    empty.mkdir()
    none = subprocess.run(["bash", "-c", _step7_script()], env={**env, "KEYS": str(empty)},
                          capture_output=True, text=True, timeout=60)
    assert none.returncode == 0 and "ok: this host has no deploy key made by this procedure" in none.stdout
    assert list(empty.iterdir()) == []


def test_the_procedures_never_make_or_grant_a_deploy_key():
    """W942 sweep: no procedure tells anyone to make, add or grant a deploy key. The connect-machine panel words
    in first-run (Part 2, step 5) wait for the operator's wording and are the one named exception."""
    panel = "references/first-run.md"
    for path in sorted(PROCEDURES.rglob("*.md")):
        text = path.read_text(encoding="utf-8")
        if path.relative_to(PROCEDURES).as_posix().endswith(panel):
            start = text.index("5. **Add this machine's keys on GitHub**")
            text = text[:start] + text[text.index("\n", start):]
        for phrase in ("Add deploy key", "### GRANT", "GRANT block", "makes the key", "ssh-keygen -q -t",
                       "grant sheet", "deploy keys are the fallback", "pushed with the deploy key"):
            assert phrase not in text, (path.name, phrase)


def test_u5_gh_is_signed_in_per_user_with_the_operators_chosen_identity():
    # W304 U5 (2026-09-25): agents open their own PRs and post verdicts with gh.
    # W371 (2026-09-28): under their owner's GitHub key. W942 (operator,
    # 2026-10-23): "this is prohibited to use on-host github", so no machine
    # account is signed in to gh and no deploy key stands in for the key.
    words = " ".join(HOST.split())
    assert "| **GitHub access** |" in HOST
    assert "### GitHub CLI for pull requests and review verdicts" in HOST
    assert "`gh` only has to be installed" in words and "Nothing is signed in" in words
    assert "**No other GitHub route (W942).**" in HOST
    assert "gh auth login" not in HOST
    assert "| 7 | **operator**, fallback only |" not in HOST
    assert "command -v gh ||" in HOST


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


def test_u2_a_new_host_can_install_a_published_release():
    # W304 U2: the host procedure offered only exact source commits, so a
    # machine that only runs agents had no published route to follow.
    step0 = HOST[HOST.index("## 0. Decide before starting"):HOST.index("## 1. Give the host agent")]
    assert "**how the client is installed**" in step0
    step2 = HOST[HOST.index("## 2. "):HOST.index("## 3. ")]
    assert '-m pip install "project-board==<approved-version>"' in step2
    assert "pb source use-release --expect-version <approved-version>" in step2
    assert "problem-board-worker/references/first-run.md" in step2
    step3 = HOST[HOST.index("## 3. "):HOST.index("## 4. ")]
    assert "pb source use-release --expect-version <approved-version>" in step3
