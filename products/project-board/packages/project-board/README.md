# project-board

The Project Board client: the `pb` command a machine runs to join a board.

## Install from reviewed source

```bash
APP_REPOSITORY=/path/to/app-ecosystem
APP_COMMIT=<approved-full-commit>
APP_EXPORT=$(mktemp -d)
KDCUBE_REPOSITORY=/path/to/kdcube
KDCUBE_COMMIT=<approved-full-commit>
KDCUBE_EXPORT=$(mktemp -d)
test "$(git -C "$APP_REPOSITORY" rev-parse "$APP_COMMIT^{commit}")" = "$APP_COMMIT"
test "$(git -C "$KDCUBE_REPOSITORY" rev-parse "$KDCUBE_COMMIT^{commit}")" = "$KDCUBE_COMMIT"
git -C "$APP_REPOSITORY" archive "$APP_COMMIT" | tar -x -C "$APP_EXPORT"
git -C "$KDCUBE_REPOSITORY" archive "$KDCUBE_COMMIT" | tar -x -C "$KDCUBE_EXPORT"

python3 \
  "$APP_EXPORT/products/project-board/packages/project-board/scripts/install_from_source.py" \
  --source-root "$APP_EXPORT" \
  --kdcube-source-root "$KDCUBE_EXPORT"
"$HOME/.local/bin/pb" procedure install --target codex --target claude-code

# After pb setup creates the target configuration:
"$HOME/.local/bin/pb" source use-code \
  --repository "$APP_REPOSITORY" \
  --ref "$APP_COMMIT" \
  --expect "$APP_COMMIT" \
  --kdcube-repository "$KDCUBE_REPOSITORY" \
  --kdcube-ref "$KDCUBE_COMMIT" \
  --expect-kdcube "$KDCUBE_COMMIT"
```

The source installer creates and smokes one complete release environment and
installs an inert user launcher for `releases/current`. Its one `pip install`
invocation resolves all six first-party distributions from the two clean
exports together with their third-party dependencies. Neither checkout is an
import path. `pb source status` reports the active release and environment,
launcher version, target receipt, both repository commits, every selected
package tree, and the source reported by the supervised relay.

## Package contents

- `project_board.client`: the `pb` command. It enrolls one coding-agent
  session as a worker, keeps that worker's relay channel, receives addressed
  mail under lease, reports against assignments, and installs the worker
  procedure into Claude Code or Codex. Credentials remain in the machine's
  native credential store; `pb` never reads them into the agent's process.
- `project_board.contract`: the references, worker identity, operation
  outcomes, plan shapes, and mail contracts that the client and the server
  both speak, defined once here.
- `project_board.procedures`: the operator, worker-host, setup, testing, and
  live-acceptance runbooks that travel with the client they operate.

The server side stays in its own repository and depends on this package for
the contract.

What the relay keeps on the host (the outbox, reconciliation receipts, mail,
leases and markers), how each store is partitioned, its retention bound and the
`relay store read` log line are in
[Storage and retention](../../../../docs/project-board/storage-and-retention.md#relay-local-state).

## Select host source

A published distribution remains a supported independent source. Install and
smoke its complete release environment, make it current, and verify the relay
restart with:

```bash
pb source use-release --expect-version 2026.09.23.0158
```

The source deployment selects one reviewed App Ecosystem commit and one
reviewed KDCube commit:

```bash
pb source use-code \
  --repository /path/to/app-ecosystem \
  --ref <commit-or-ref> \
  --expect <full-40-character-commit> \
  --kdcube-repository /path/to/kdcube \
  --kdcube-ref <commit-or-ref> \
  --expect-kdcube <full-40-character-commit>
```

The code release is exported from Git objects and verified blob by blob. It
contains `project-board`, `app-foundation`, `service-foundation`,
`connection-hub`, and `connection-hub-cli` from the App Ecosystem commit, plus
`kdcube-cli` from the KDCube commit. Their named commits and package trees form
one path-independent release ID. One host-wide `releases/current` pointer is
shared by the `pb` launcher and every relay definition; each target keeps its
source-action receipt. An installed relay must report the candidate source
before the switch succeeds. A failed start restores the previous current
release and target receipt.

Running `python -m project_board.client.entrypoint` with checkout package paths
on `PYTHONPATH` is an explicit development process. It does not change the
host selector, and `pb status` reports that process as unpinned checkout code.

## Why it is packaged

A machine that runs agents installs one package family and selects one durable
source manifest. Commands and the relay therefore load the same reviewed
implementation, while the release ID, repository commits, and package-tree
identities give deployment and status checks precise facts to compare.
