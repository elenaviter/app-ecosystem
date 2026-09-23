---
id: project-board.worker-reference.runtime-actions
title: Runtime Actions By Tree
summary: Which runtime action makes a change live, by the tree the change is in, and how to verify it in the running process rather than the checkout.
tags: [procedure, problem-board, worker, runtime, deploy]
keywords: [kdcube refresh, --build, bundle reload, bundle config apply, maintainer-local-python-package, widget dist, what loaded, ready with a constraint, verify the artifact]
see_also: []
---

# Runtime Actions By Tree

Read this before asking the coordinator for a reload, refresh or restart.
Name the action by the tree the change is in; the wrong one reports a fix as
live that has never executed.

| You changed | It reaches the runtime when | Not enough |
| --- | --- | --- |
| Platform or SDK Python (`kdcube_ai_app/...`), including server-rendered OAuth consent pages | `kdcube refresh --path "$REPO" --build` | bare `refresh --build`, which rebuilds the old staged copy; restarting containers |
| A package from `app-ecosystem` used inside KDCube (`project-board`, `app-foundation`, `connection-hub`) | the same refresh, with every such distribution staged in the SAME build through `--maintainer-local-python-package DIST=SOURCE` | rebuilding without the selector, which keeps the published version |
| Widget `src/` | the same refresh, or a bundle reload for the widget's bundle; the pipeline builds `dist/`, and the reload returns before that build finishes | editing `src/` alone, building widgets by hand, or reading the reload receipt as the widget being live |
| Descriptor content (`bundles.yaml`) | `bundle config apply` or `bundle reload <bundle-id>` | `refresh`, which preserves `$WORKDIR/config` |
| An app under `apps/` | a bundle reload | nothing further |
| A released Problem Board host client | install the exact approved `project-board` version, then run `pb source use-release --expect-version <version>`; the source action records the version and restarts the relay when it is installed | upgrading the package alone, because the recorded version remains unchanged and ordinary commands refuse the mismatch |
| A committed Problem Board client under development | install the six first-party distributions together from clean App Ecosystem and KDCube exports, then run `pb source use-code` with both repository paths, refs, and full approved commits; it exports those six packages as one composite release, atomically selects them for the command and relay, restarts the installed relay, and accepts only the new process's matching startup record | independent installs, an editable install, a live-checkout launcher, or selecting only one repository or the relay |
| The already selected Problem Board relay source | `pb relay-service restart`. A relay restart is host-local and reloads the recorded source without advancing it | a bundle reload; a restart cannot select a newer checkout or package version |
| The worker procedure package inside `project-board` | `pb procedure install`, run by the coordinator on the host after the selected release or code commit carries the new revision | editing package source, which installed sessions never read |

Copying a file into a container and restarting a container are not actions
this team has: a container-local patch is invisible to everyone, vanishes
without warning, and makes the running system disagree with the repository
while every test still passes.

## Develop Without Moving The Host Source

A maintainer may run the client from an App Ecosystem checkout on purpose:

```bash
PYTHONPATH=<project-board-src>:<app-foundation-src>:<service-foundation-src>:<connection-hub-src>:<connection-hub-cli-src>:<kdcube-cli-src> \
  python -m project_board.client.entrypoint status
```

That process runs the checkout directly and `pb status` reports
`client.pinned: false` with `source.mode: checkout`, the Project Board
checkout's commit, and whether its owned paths are dirty. Checkout mode makes
no durable claim about the other import paths in that process. It does not
rewrite the per-target source selector and does not affect the supervised
relay. Use this path for tests and diagnosis. Use `pb source use-code` only
when the operator or coordinator has approved moving the host's pinned source
to two reviewed commits.

`pb source status` separates three facts: the released bootstrap installed on
the host, the exact selected version or composite release, and the source
reported by the running relay. A code selector names both full commits and the
tree id of every exported package. A running relay observes a new selector only when restarted;
the source command performs that restart and rolls the selector back when the
new process does not report the expected source.

## Cut Over A Host That Still Runs The Checkout Client

The package family must exist in the relay interpreter before a shared checkout
stops providing `pb`. Read `program_arguments[0]` from `pb relay-service
status`; it is the relay interpreter. Prepare clean exports from the approved
App Ecosystem and KDCube commits, then install every first-party distribution
in one resolution:

```bash
APP_REPOSITORY=<app-ecosystem>
APP_COMMIT=<approved-full-commit>
APP_EXPORT=$(mktemp -d)
KDCUBE_REPOSITORY=<kdcube>
KDCUBE_COMMIT=<approved-full-commit>
KDCUBE_EXPORT=$(mktemp -d)
test "$(git -C "$APP_REPOSITORY" rev-parse "$APP_COMMIT^{commit}")" = "$APP_COMMIT"
test "$(git -C "$KDCUBE_REPOSITORY" rev-parse "$KDCUBE_COMMIT^{commit}")" = "$KDCUBE_COMMIT"
git -C "$APP_REPOSITORY" archive "$APP_COMMIT" | tar -x -C "$APP_EXPORT"
git -C "$KDCUBE_REPOSITORY" archive "$KDCUBE_COMMIT" | tar -x -C "$KDCUBE_EXPORT"

PB_VENV=$(dirname "$(dirname "<relay-python>")")
python3 \
  "$APP_EXPORT/products/project-board/packages/project-board/scripts/install_from_source.py" \
  --source-root "$APP_EXPORT" \
  --kdcube-source-root "$KDCUBE_EXPORT" \
  --venv "$PB_VENV"
"<relay-python>" -c 'import json; from project_board.client.source_control import installed_release_source; print(json.dumps(installed_release_source(), sort_keys=True))'
"<relay-python>" -m project_board.client.entrypoint source use-code \
  --repository "$APP_REPOSITORY" --ref "$APP_COMMIT" \
  --expect "$APP_COMMIT" \
  --kdcube-repository "$KDCUBE_REPOSITORY" --kdcube-ref "$KDCUBE_COMMIT" \
  --expect-kdcube "$KDCUBE_COMMIT"
"<relay-python>" -m project_board.client.entrypoint source status
```

Before fast-forwarding the old checkout:

1. verify that the installed-source line reports `mode: released` and the
   expected package version;
2. verify `source.mode: snapshot`, the release ID, both full commits, all six
   package trees, and the restarted relay's matching startup record with the
   same interpreter's `source status`;
3. verify that the guarded user launcher created by the source installer enters
   that same environment, then install and verify the worker procedure from the
   selected snapshot;
4. only then fast-forward or remove the checkout implementation.

`use-code` performs the coordinated relay restart. The fast-forward follows
the successful source verification so the command remains available
throughout the cutover.

## Relay Restart Is Host-Local

Each machine runs one relay, and it carries only that machine's channels. A
relay restart is host-local. The agents on that host agree first. The
coordinator on that host restarts it, and on a host without a coordinator the
agents there pick one of themselves to do it. Agents on other hosts are not
affected and need not agree. `pb worker list` lists the workers on your host,
which are the agents who must agree.

`pb source use-code` and `pb source use-release` include a relay restart when
the service is installed, so they follow this same agreement before the source
action begins. A selector change made before relay installation records
`relay_restart_required: true`; installing the canonical service later starts
it from that selection.

To agree: announce what you restart and why with a shared-write entry of kind
`relay_restart` whose target names the host (for example `host:development-one`,
summary "I am restarting the relay: <why>"), and mail each agent on that host.
Collect their ready: a worker mid-call or holding an uncommitted relay patch
says wait. Then restart, report the result, and clear the entry.

**Verify in the running artifact, not in the checkout.** A green suite says the
source is correct and nothing about what is running, and a commit hash says
what was asked for and nothing about what was staged. Ask the running process
for a symbol or behaviour the change introduced: the relay's first stamped
line plus `pb source status`, `dist/` inside the container after the widget
build, `pb procedure verify` for the package. A bundle reload reports how many modules it evicted;
that count is about the bundle and says nothing about platform packages, so a
reload never applies a change to one of them.

**Ask what it released.** An action stages the working tree at that instant,
so the commits it made live are the ones on that tree since the last action,
not only yours. Name the commit you need live when you ask, and after the
action the coordinator says the range that loaded; a `ready` may carry a
constraint (a commit it must be at or after, a window, a file you are about
to touch), and the coordinator honours it or re-announces.

Use the KDCube release's own operating documentation for the exact refresh
flags supported by that installed version.

## Client Source Selection

Selecting the client source is a runtime action of the same kind as a relay
restart, and follows the same agreement on the host. `pb source use-release`
selects an approved package version and `pb source use-code` selects exact
App Ecosystem and KDCube commits as one release for both the command and the
relay, and either includes the host-local restart the relay needs to observe
it. A direct checkout invocation is a development process: it must remain
visibly unpinned and never changes the host selector. Why: the command and
the relay have to run the same source, and a selection nobody announced looks
to the other agents like a relay that changed by itself.
