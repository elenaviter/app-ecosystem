---
id: app-ecosystem.project-board.procedure.install-update-rollback
title: Install, Update, Roll Back pb
summary: How a machine gets pb from the package index, moves to a newer version, returns to one it ran before, and shows what runs now; each step names who does it and what they should see.
tags: [procedure, problem-board, install, update, rollback, release]
keywords: [pip install project-board, pb source versions, pb source use-release, pb source status, rollback, bootstrap venv, ~/.local/bin/pb]
see_also:
  - ./first-time-setup.md
  - ./add-a-worker-host.md
  - ./problem-board-worker/references/first-run.md
  - repo:app-ecosystem/products/project-board/packages/project-board/README.md
  - repo:app-ecosystem/docs/releases.md
---

# Install, Update, Roll Back pb

`pb` comes from the package index as `project-board`, with the three packages
it depends on at the same version. No repository checkout and no commit hash
are needed. Every version a machine runs is installed in its own complete
environment under `~/.kdcube/client-runtime/tools/problem-board/releases`, and
`~/.local/bin/pb` points at the active one. The machine's relay runs the same
version as `pb`.

Changing the version restarts the machine's relay, which every agent on the
machine shares. On a machine with agents, the coordinator announces the
window first, and each agent restarts its session afterwards so it loads the
matching procedure.

## What runs now

*Anyone on the machine.*

```bash
pb source status
pb source versions
```

`pb source status` shows the active release, its version, and the version the
relay reports. `pb source versions` lists the versions on the package index,
newest first, and marks each as installed on this machine and active. It
prints the `use-release` line for the newest version, or for the one named by
`--version <version>`. It changes nothing.

## First install

*The person who sets up the machine, or their agent with their approval.*

```bash
python3 -m venv ~/.local/share/project-board-bootstrap
~/.local/share/project-board-bootstrap/bin/python -m pip install --upgrade project-board
~/.local/share/project-board-bootstrap/bin/pb --version
```

You should see `problem-board <version>`. Keep that version. Then configure the
machine with `pb setup` (the values come from whoever runs your board; see
[first-time setup](first-time-setup.md) section 3), and make that version the
machine's own:

```bash
~/.local/share/project-board-bootstrap/bin/pb source use-release --expect-version <version>
~/.local/bin/pb procedure install --target claude-code --target codex
pb relay-service install
```

`use-release` builds and checks a complete environment for that version,
installs `~/.local/bin/pb`, and selects the version for the relay. You should
see the release installed and selected; `pb source status` then names that
version. `~/.local/bin` must be on your `PATH`: if `pb --version` answers
`command not found`, add
`export PATH="$HOME/.local/bin:$PATH"` to `~/.zshrc` (zsh) or `~/.bashrc`
(bash) and open a new terminal. The bootstrap environment is only the first
loader; after this step every command is plain `pb`.

## The install lines on a machine that is already set up

*Whoever runs the Connect-a-machine lines on that machine.*

The same lines work on a new machine and on one that already runs `pb`; the
person does not need to know which (operator, 2026-10-05: "the user should
have no any idea if this is new install or no. it simply must work smoothly
and easy. with couple of lines." and "i asked many times to maek the client
install fully functional according to the "connect the machine" tutorial. for
both from soucres and from release mode."). On a machine that is set up and
runs another client, `pb procedure install` run by the package just installed
switches the whole machine to that package before it installs the skill:

- installed from the package index: `pb source use-release` at that version;
- installed from an App Ecosystem checkout (`pip install <checkout>/...`):
  `pb source use-code` at that checkout's `HEAD`.

The release environment, `~/.local/bin/pb`, the relay and the selection move
together, as with `pb source`, so the relay restarts. The skill and the Claude
Code hooks then name `~/.local/bin/pb`. Its result names the switch
(`switched`: `source`, the version or commit, and the `previous` selection).
Nothing changes on a machine that is not set up yet, on one that already runs
this package, or when `pb procedure install` runs inside a selected release
(`~/.local/bin/pb procedure install` after `pb source use-code` keeps that
snapshot). A checkout whose client packages have uncommitted changes is
refused with `work_client_install_not_a_commit` and the changed files: the
machine can only run a commit.

## First switch from a code snapshot

*The coordinator, in an announced window, with the machine's agents agreed.*

A machine that runs a code snapshot selected with `pb source use-code` moves
to a published version the same way:

```bash
pb source versions
pb source use-release --expect-version <version>
pb procedure install --target claude-code --target codex
```

You should see the relay report the new version in `pb source status`. The
snapshot stays installed, so the machine can return to it with the same
`use-code` command it was selected with.

## Update to a newer version

*The coordinator, in an announced window, with the machine's agents agreed.*

```bash
pb source versions
pb source use-release --expect-version <newer version>
pb procedure install --target claude-code --target codex
```

Take the line `pb source versions` prints. `use-release` installs the new
version beside the current one, checks it, switches `pb` and the relay to it,
and waits for the relay to report it. If the relay does not come back on the
new version, the previous one is put back and the command says so. Then
install the procedure again so the agents read the one that matches, and each
agent restarts its session in place.

## Roll back to a version you ran before

*The coordinator, in the same kind of window.*

```bash
pb source versions
pb source use-release --expect-version <previous version>
pb procedure install --target claude-code --target codex
```

The most recent releases stay installed (the active one and the last few), so
returning to one of them switches without a download. `pb source versions`
marks which are installed. An older version is installed again from the index
first. A version from before the client split (2026-09-26) is not a supported
roll-back target.

## When something is wrong

- `pb source status` names a different version for the relay than for `pb`:
  the relay did not restart on the new one. Run `pb relay-service status`,
  then the same `use-release` line again.
- `use-release` refuses because the version is not on the index: check the
  version with `pb source versions`; PyPI can take some minutes to serve a new
  release.
- `pb source versions` cannot reach the index: it names the index it asked.
  `PIP_INDEX_URL`, or `--index-url`, points it at another one.

**After a reboot of a headless Linux host, or an agent approved whose watch
never hears it (W558).** The relay keeps agents' credentials in the user's
password store, and a reboot locks it. Until it is unlocked, every agent on the
host is cut off, and a newly approved agent's channel stays
`pending_authorization` (`pb relay-service status`) while the relay log shows
`credential_store_locked`, `oauth_credential_custody_timeout` or, on an older
client, `oauth_profile_lock_timeout`. The fix is add-a-worker-host step 6:
the operator unlocks (unlock, check, reset there, and nothing else). The relay
then recovers on its next attempt, with no restart; a client older than this
change needs one relay restart after the unlock.
