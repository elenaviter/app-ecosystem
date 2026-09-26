---
id: project-board.worker-reference.runtime-actions
title: "Runtime Actions: Project Runtimes And Problem Board Host Actions"
summary: How a project's runtimes and their profiles carry the commands for a runtime action, which Problem Board host action makes a pb change live (client source, relay restart, procedure install), and how to verify it in the running process rather than the checkout.
tags: [procedure, problem-board, worker, runtime, deploy]
keywords: [project runtimes, runtime profile, local_profile, releases, pb source use-release, pb source use-code, relay restart, procedure install, what loaded, ready with a constraint, verify the artifact]
see_also: []
---

# Runtime Actions: Project Runtimes And Problem Board Host Actions

## Project Runtimes

A project declares its runtimes: each named place its system runs, the host
its actions are triggered from, each action with who may trigger it and the git
ref it releases, and a profile that holds the commands for that kind of
runtime. `pb worker context` returns them as `runtimes`, read from the
project's setup (by hand in `project-setup.json` at the journal home's root,
later on the project's Control Card). The four concepts behind them are in
[projects, runtimes and refs](repo:app-ecosystem/docs/project-board/projects-runtimes-and-refs.md).

- **A runtime action releases its refs.** It names, for each repository it
  loads (a platform refresh loads the platform and the packages it stages, an
  app reload its app), the ref it releases (`releases`). The commit each ref
  names is fetched onto the runtime's machine and loaded; never whatever a
  working tree holds at that moment. Before it, the coordinator integrates the
  commits to go live onto that ref and pushes it ([coordinator](coordinator.md),
  Reload, refresh, restart, step 1).
- **Its result names what loaded:** each repository, its ref, and the commit
  the ref named when it loaded. A result that names another commit is a failed action.
- **Only who the action names triggers it**, from the runtime's host.
- **The commands are the profile's.** Read the runtime's `local_profile` for
  them. A project that declares no runtime has no runtime actions, and a
  runtime of another kind has its own profile.

The Problem Board host actions below (relay restart, client source, procedure
install) belong to every project that runs `pb`. A runtime's own actions
(a platform refresh, an app reload, a deploy) and the order and receipts they
need are in that runtime's profile, never on this page. A project with no
runtime has only the host actions. For example, the KDCube deployment this
team maintains names the
[KDCube maintainer runtime profile](repo:app-ecosystem/products/kdcube/procedures/runtime-profile-maintainer.md).

Read this before asking the coordinator for a reload, refresh or restart.
Name the action by the tree the change is in and the runtime that runs it;
the wrong one reports a fix as live that has never executed.

| You changed | It reaches the runtime when | Not enough |
| --- | --- | --- |
| A released Problem Board host client | `pb source use-release --expect-version <version>` builds a new release environment, resolves that version's complete dependency graph, smokes its `pb --version` and imports, atomically activates it, and verifies the restarted relay | upgrading a permanent bootstrap environment, which leaves the launcher and relay on a different dependency set |
| A committed Problem Board client under development | `pb source use-code` with both repository paths, refs, and full approved commits exports all six first-party packages, resolves them together inside the composite release environment, smokes it, atomically activates it, and accepts only the restarted relay's matching startup record | independent installs, an editable install, a live-checkout launcher, or selecting only one repository or the relay |
| The already selected Problem Board relay source | `pb relay-service restart`. A relay restart is host-local and reloads the recorded source without advancing it | a runtime action such as a reload; a restart cannot select a newer checkout or package version |
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

## One Complete Host Release

One host runs one Project Board client release. Every complete release lives at
`~/.kdcube/client-runtime/tools/problem-board/releases/<release-id>/` and owns
its source identity, `venv`, first-party packages, and resolved third-party
dependencies. `releases/current` is the one atomic host pointer. The generated
`~/.local/bin/pb` launcher contains no selection logic: launcher version 2 sets
`PROBLEM_BOARD_INVOKED_PB` and executes
`releases/current/venv/bin/pb`. Every relay service definition executes the
same stable `current/venv/bin/python` path.

Each target keeps its own `selection.json` receipt. A host switch writes the
same selected source to every configured target receipt; those files are
synchronized evidence rather than independent selectors. This division lets
`pb --config <target>` report durable evidence while the launcher and every
relay on the host use one complete dependency set.

A switch has four phases under one host activation lock:

1. export or identify the candidate, create its `venv`, resolve the complete
   dependency graph, run candidate `pb --version` and `pip check`, and import
   the candidate-owned CLI, relay, authorization, Connection Hub, and
   foundation modules with checkout import paths removed; source builds also
   import every package named by the source manifest;
2. stop every installed relay whose definition uses the host's stable current
   path;
3. atomically move `releases/current`, write every configured target receipt,
   and install the inert launcher;
4. restart every installed relay and accept the switch only when every new
   startup record names the candidate source, then retain the three most
   recently activated complete environments.

A build or smoke failure occurs before relays stop and leaves `current`, every
receipt, the launcher, and all running relays unchanged. A stop, activation,
restart, or startup-record failure restores the exact previous current release,
each target's previous receipt or absence of one, and the previous launcher,
then attempts to restart and verify every former relay; one failed restart does
not prevent the remaining relays from being attempted, and the rollback receipt
lists every failure. If a candidate relay cannot be stopped, rollback reports
that failure and does not move `current` while that process could still load
modules from it. Pruning begins only after every relay has verified the
activation.

`pb source status` reports the active release ID and path, environment commands,
launcher path and version, this target's receipt, the source loaded by the
command, and every discovered host relay's status. A code receipt names both
full commits and the tree ID of every exported package.

## Move An Existing Host To Release Environments

An existing host may run either a checkout client or the former long-lived
`~/.kdcube/client-runtime/tools/problem-board-venv`. Keep that source and
environment in place while preparing clean exports from approved App Ecosystem
and KDCube commits. The source installer invokes the same release builder as
`source use-code` and `source use-release`: it builds and smokes a complete
candidate before activating `releases/current`, then replaces the user launcher
with launcher version 2.

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

python3 \
  "$APP_EXPORT/products/project-board/packages/project-board/scripts/install_from_source.py" \
  --source-root "$APP_EXPORT" \
  --kdcube-source-root "$KDCUBE_EXPORT"
"$HOME/.local/bin/pb" --version
"$HOME/.local/bin/pb" relay-service install
"$HOME/.local/bin/pb" source use-code \
  --repository "$APP_REPOSITORY" --ref "$APP_COMMIT" \
  --expect "$APP_COMMIT" \
  --kdcube-repository "$KDCUBE_REPOSITORY" --kdcube-ref "$KDCUBE_COMMIT" \
  --expect-kdcube "$KDCUBE_COMMIT"
"$HOME/.local/bin/pb" source status
```

The relay service install is required once during this migration because its
old definition names the former interpreter. Every later source switch keeps
the same `releases/current/venv/bin/python` service command and performs its own
verified restart.

On a host that already has relay definitions using `releases/current`,
`~/.local/bin/pb source use-code` owns every later code-source change. The
bootstrap installer detects those installed current-path relay units and exits
before building or activating a candidate, directing the operator to the
host-wide source transaction instead.

Before fast-forwarding a checkout or deleting
`problem-board-venv`, verify all of these facts for every configured target:

1. `launcher.version` is `2`, `launcher.current` is true, and the active
   environment is below `releases/<release-id>/venv`;
2. `relay.program_arguments[0]` is the stable
   `releases/current/venv/bin/python` path;
3. the target receipt and relay startup record name the same released version
   or composite source, including both commits and all six package trees for a
   code release;
4. `pb procedure verify` succeeds from the new launcher.

At that point the former venv has no launcher or service consumer and may be
deleted. The source checkout remains a build input for future reviewed
`use-code` actions; it is never an import path for the running client.

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
line plus `pb source status`, `pb procedure verify` for the package, and for a
runtime's action the receipt and the checks its profile gives, compared with
the commit the ref named before anything else.

**Ask what it released.** Every action is addressed to a commit and releases
that commit, so the commits it made live are the ones between the last
activated commit and this one, not only yours. Name the commit you need live
when you ask, pushed to the ref the action releases, and after the
action the coordinator says the range that loaded; a `ready` may carry a
constraint (a commit it must be at or after, a window, a file you are about
to touch), and the coordinator honours it or re-announces.

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
