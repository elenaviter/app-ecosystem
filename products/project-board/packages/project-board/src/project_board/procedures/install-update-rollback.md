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
