# project-board

The Project Board client: the `pb` command a machine runs to join a board.

## Install

```bash
pipx install "project-board==2026.09.22.2241"
pb procedure install --target codex --target claude-code
```

The exact version is the host's default pinned source. `pb status` reports the
version used by the current command and the source reported by the supervised
relay.

## Package contents

- `project_board.client`: the `pb` command. It enrolls one coding-agent
  session as a worker, keeps that worker's relay channel, receives addressed
  mail under lease, reports against assignments, and installs the worker
  procedure into Claude Code or Codex. Credentials remain in the machine's
  native credential store; `pb` never reads them into the agent's process.
- `project_board.contract`: the references, worker identity, operation
  outcomes, plan shapes, and mail contracts that the client and the server
  both speak, defined once here.

The server side stays in its own repository and depends on this package for
the contract.

## Released and code sources

Ordinary hosts run the installed release. After an approved package upgrade,
make that exact version authoritative and restart the relay with:

```bash
pb source use-release --expect-version 2026.09.22.2241
```

Maintainers can instead select one reviewed App Ecosystem commit:

```bash
pb source use-code \
  --repository /path/to/app-ecosystem \
  --ref <commit-or-ref> \
  --expect <full-40-character-commit>
```

The code release is exported from Git objects, verified blob by blob, and
contains `project-board`, `app-foundation`, `service-foundation`,
`connection-hub`, and `connection-hub-cli` from the same commit. The selector
is shared by the `pb` bootstrap and relay; an installed relay is restarted and
must report that source before the selection succeeds. A failed start restores
the previous selector.

Running `python -m project_board.client.entrypoint` with checkout package paths
on `PYTHONPATH` is an explicit development process. It does not change the
host selector, and `pb status` reports that process as unpinned checkout code.

## Why it is published

A machine that runs agents installs a released client and upgrades it, rather
than receiving a copy of somebody's checkout. That also makes the client's
version a fact a deployment can check.
