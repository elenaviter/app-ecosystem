---
id: project-board.worker-reference.runtime-actions
title: "Runtime Actions: Project Runtimes And Problem Board Host Actions"
summary: How a project's runtimes and their profiles carry the commands for a runtime action, which Problem Board host action makes a pb change live (client source, relay restart, procedure install), and how to verify it in the running process rather than the checkout.
tags: [procedure, problem-board, worker, runtime, deploy]
keywords: [runtime-window backup, pb worker backup, backup manifest, keep the newest backup, project runtimes, runtime profile, local_profile, releases, pb source use-release, pb source use-code, relay restart, procedure install, what loaded, ready with a constraint, verify the artifact]
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
[projects, runtimes and refs](repo:app-ecosystem/products/project-board/docs/projects-runtimes-and-refs.md).

- **A runtime action releases its refs.** It names, for each repository it
  loads (a platform refresh loads the platform and the packages it stages, an
  app reload its app), the ref it releases (`releases`). The commit each ref
  names is fetched onto the runtime's machine and loaded; never whatever a
  working tree holds at that moment. Before it, the merger the item's route names
  (the coordinator when none is named) integrates the commits to go live onto
  that ref and pushes it ([coordinator](coordinator.md), Reload, refresh,
  restart, step 1; [collaboration](collaboration.md) Rule 2).
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
| A committed Problem Board client under development | `pb source use-code` with the App Ecosystem repository path, ref, and full approved commit exports all four first-party packages, resolves them together inside the release environment, smokes it, atomically activates it, and accepts only the restarted relay's matching startup record | independent installs, an editable install, a live-checkout launcher, or selecting only one repository or the relay |
| The already selected Problem Board relay source | `pb relay-service restart`. A relay restart is host-local and reloads the recorded source without advancing it | a runtime action such as a reload; a restart cannot select a newer checkout or package version |
| The worker procedure package inside `project-board` | `pb procedure install`, run on each host by the installer the item's route names (otherwise the coordinator on that host, or its elected integrator; [collaboration](collaboration.md) Rule 2) after the selected release or code commit carries the new revision | editing package source, which installed sessions never read |

Copying a file into a container and restarting a container are not actions
this team has: a container-local patch is invisible to everyone, vanishes
without warning, and makes the running system disagree with the repository
while every test still passes.

## Runtime-Window Database Backups

The coordinator who runs a runtime window backs up the tables the action can
change before it, into the host's managed backup folder, never an agent's
scratch folder, and keeps only the newest once the window's ALL CLEAR is
verified.

1. **Name the file.** `pb worker backup --project-ref <project> --new --dump-format plain-sql-gzip`
   prints where to write it: `<backup root>/<project id>/pb-backup-<UTC time>.sql.gz`,
   in a folder only the host user can read. Write the dump there with the
   runtime profile's dump command.
2. **Record and verify it before the action.**
   `pb worker backup --project-ref <project> --record <file> --dump-format plain-sql-gzip --label "<action> at <commit>"`
   checks the file and adds it to the folder's manifest. A plain SQL gzip dump
   is checked by reading the whole gzip stream (CRC and length) and requiring
   pg_dump's header, its completion marker and at least one `CREATE TABLE`;
   `pg_restore --list` cannot read a plain dump. A custom-format dump
   (`pg_dump -Fc`, `--dump-format pg-custom`) is checked with `pg_restore --list`
   (`--pg-restore "docker exec -i <container> pg_restore"` when PostgreSQL
   runs in a container). A failed check stops the window before the action.
   Either check proves integrity, never that the backup restores: report
   "integrity verified", and call something a restore test only after
   restoring it into a scratch database.
3. **Prune after the ALL CLEAR.** Once the window's ALL CLEAR is verified,
   `pb worker backup --project-ref <project> --prune --all-clear "<its receipt or message ref>"`
   shows what would go, and `--apply` keeps only the newest backup and deletes
   the older ones the manifest lists. It refuses when the newest one failed
   its check or changed since it was recorded, so the copy kept is always a
   verified one.

The operator chooses the backup root per host
(`pb host configure --backup-root <absolute path outside every Git tree>`);
until then `--new` refuses and nothing is created. The manifest is the
coordinator's: any agent may list it (`pb worker backup --project-ref <project>`),
only the coordinator names, records and prunes backups. Pruning never touches
a file the manifest does not list; the listing names such files, and older
dumps in agents' scratch folders stay their owners' until the operator decides
how to clear them.

Why: every window left a table dump in the scratch folder of whichever agent
ran it, and nothing removed them. On 2026-09-30 one host held 24 dumps in one
agent's scratch folder and 9 in another's, and its disk filled. The operator's
rule: keep the newest one.

## Develop Without Moving The Host Source

A maintainer may run the client from an App Ecosystem checkout on purpose:

```bash
PYTHONPATH=<project-board-src>:<app-foundation-src>:<service-foundation-src>:<connection-hub-src> \
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
command, and every discovered host relay's status. A code receipt names the
full App Ecosystem commit and the tree ID of every exported package (a release
selected before W322 also names its KDCube commit).

## Move An Existing Host To Release Environments

An existing host may run either a checkout client or the former long-lived
`~/.kdcube/client-runtime/tools/problem-board-venv`. Keep that source and
environment in place while preparing a clean export of an approved App
Ecosystem commit. The source installer invokes the same release builder as
`source use-code` and `source use-release`: it builds and smokes a complete
candidate before activating `releases/current`, then replaces the user launcher
with launcher version 2.

```bash
APP_REPOSITORY=<app-ecosystem>
APP_COMMIT=<approved-full-commit>
APP_EXPORT=$(mktemp -d)
test "$(git -C "$APP_REPOSITORY" rev-parse "$APP_COMMIT^{commit}")" = "$APP_COMMIT"
git -C "$APP_REPOSITORY" archive "$APP_COMMIT" | tar -x -C "$APP_EXPORT"

python3 \
  "$APP_EXPORT/products/project-board/packages/project-board/scripts/install_from_source.py" \
  --source-root "$APP_EXPORT"
"$HOME/.local/bin/pb" --version
"$HOME/.local/bin/pb" relay-service install
"$HOME/.local/bin/pb" source use-code \
  --repository "$APP_REPOSITORY" --ref "$APP_COMMIT" \
  --expect "$APP_COMMIT"
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
   or code source, including the App Ecosystem commit and all four package trees
   for a code release;
4. `pb procedure verify` succeeds from the new launcher.

At that point the former venv has no launcher or service consumer and may be
deleted. The source checkout remains a build input for future reviewed
`use-code` actions; it is never an import path for the running client.

## Relay Restart Is Host-Local

Each machine runs one relay, and it carries only that machine's channels. A
relay restart is host-local. The agents on that host agree first. The
installer the route names for that host restarts it; when none is named, the
coordinator on that host, or on a host without one its elected integrator
([collaboration](collaboration.md) Rule 2). Agents on other hosts are not
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
Collect an explicit ready from each affected active session using
[Host Client Window Quiescence](#host-client-window-quiescence), including the
installer's other work. Silence is not ready. A worker that is unavailable is
recorded as pending with its reason; it is not exempt from that quiescence
gate. Its absence proves neither that a call ended nor that another cannot
start ([collaboration](collaboration.md) Rule 10). The restart reloads the
recorded source, so nothing in an isolated worktree changes the candidate.
Then restart, report the result, and clear the entry.

## Host Client Window Quiescence

This is the additional gate for a host's client-source switch or relay
restart, not for a runtime-only release from an isolated exact-commit tree.
READY means calls drained and held **before** the switch: finish or safely
stop conflicting work and make no new PB/relay calls or project mutations
until the window's ALL CLEAR or explicit cancellation. Before execution,
only the window's readiness/control exchange is allowed; after acknowledging
STARTING NOW, end the turn in a waiting state and issue no commands. Passive
notification transport continues. A queued START wake cannot stop a busy
model response and is never the mechanism that establishes the hold.
The final acknowledgement is the session's last control call, after its
leases and earlier calls are settled. If a reply is due before settlement,
send a non-final status, settle, then acknowledge the drained hold. Only the
named installer executes the planned window and its prescribed verification
and control exchange; it starts no unrelated work while the hold is active.

**Who the window asks and waits for.** A relay restart or client switch
affects every agent on the machine, but agents of different projects cannot
yet message each other, and no per-machine upgrade procedure exists. Until
one does, a machine upgrade initiated in a project does this:

1. **It informs and waits for the available agents of its own team only.**
   The affected sessions are this project's team members on this host that
   the team availability read shows available now (`pb worker context`, the
   coordinator reference's "What the coordinator is for"): attending, active
   pool, quota left, session online and in sync. **Available, not all:** an
   agent that is out of tokens, offline, out of sync or not on this team is
   not asked and not waited for, and is recorded as not asked with its reason.
2. **It tells the operator about the other agents on this machine.** When
   other agents run on the host that do not belong to the initiating team,
   the installer sends the operator a `decision` (it reaches their Telegram)
   before the window, naming the host, the window id and its time, and each
   such agent to inform (its alias, stable name and project). The window
   does not wait for them.

The installer names the host, stable affected session identities, exact
candidate, window correlation, rollback owner and a bounded UTC
acknowledgement deadline in the announcement. For each session, record on
the window's item either **acknowledged STARTING NOW** (its explicit reply
that calls are drained and it will remain waiting) or an evidenced
**idle/waiting session state** under this same window's no-calls hold.
A presence label, transport heartbeat, send receipt, lease settlement alone,
or a quiet relay at one instant proves neither that the model handled the
request nor that future calls are held. Transport acceptance is not session handling.
If the runtime cannot establish that waiting state, require the explicit
acknowledgement; do not infer it. READY is not permission to keep working
until the next wake, and an earlier READY for another window is not reusable.

An affected (available) session that is busy, answers HOLD or misses the
acknowledgement without that evidence is **non-quiesced**, with its reason,
last evidence, clearing actor and deadline recorded. Ask an available missing participant once more before the deadline.
At timeout, record **window not started**, cancel and release already-held
participants on the same channel, or re-announce a new bounded window after
the blocker clears. A deadline is not consent. Never waive this gate for an
affected session because the release tree is clean or no call is currently
visible. A session the availability read shows unavailable, or one outside
the team, is not an affected session (point 1 above); why: windows waited on
agents that were out of team or could not answer, and were cancelled five
times in one day (operator, 2026-10-04: "no one must wait for agents that are
out of team or offline"). The machine-restart freeze below remains stronger.

**Every required START send must return OK (exit zero). Any refusal or unknown
send outcome stops the window before execution.** Use a body file for multiline
prose; never continue a shell loop past a failed send. This checked example
uses the announced `project_ref`, `work_ref`, `window_id`, prepared
`start_body_file` and nonempty `affected_workers` array of stable identities:

<!-- host-start-send-guard -->
```bash
announce_host_start() {
  if [ "$#" -eq 0 ]; then
    printf 'Window not started: affected-session inventory missing\n' >&2
    return 1
  fi
  for worker in "$@"; do
    if pb worker send --project-ref "$project_ref" --work-ref "$work_ref" \
      --recipient "$worker" --kind update --subject "STARTING NOW: $window_id" \
      --body-file "$start_body_file" --correlation-id "$window_id" \
      --idempotency-key "$window_id-start-$worker" --format brief; then
      :
    else
      printf 'Window not started: START send failed for %s\n' "$worker" >&2
      return 1
    fi
  done
}
announce_host_start "${affected_workers[@]}" || exit 1
```

Successful sends only permit the next **acknowledgement/evidence check**, not
execution. Execute only after every affected session meets the quiescence
gate above and the action's other preflight gates hold. A failed send cancels
the unstarted window; notify already-held participants rather than leaving
them waiting. Do not retry an unknown write with a new identity: resolve it
under its original identity and idempotency key.

After execution, the ALL CLEAR records the actual UTC switch interval and
either zero calls within it or each overlapping call, its effect/outcome and
recovery. Unknown writes retain their original identity and idempotency key.
Source tests prove the procedure/guard only; they do not prove a real window
was quiescent, that an installer adopted it, or that every call was accounted for.

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

## A Machine Restart Freezes Every Agent On It

A restart of the machine itself stops every agent session, relay and
runtime on it, so it is a window of its own: the coordinator owns it and the
operator reboots. READY in this window means quiescent. The agent has pushed
its work in progress, settled its leases and written where it resumes, and
then starts nothing until the operator says the machine is back or cancels
the window: no tests, builds, merges, item edits or periodic checks after
READY, however small. Two passive things continue: the inbox watch of a
Claude Code session (its Stop hook requires it, and it changes nothing, while
a Codex session has none and is woken by its relay) and receiving and
settling mail, answered with "frozen until the restart" when it asks for
work. An operation already running that cannot stop safely is reported at
once, in place of READY. After the reboot the resume is recorded on the
window's item in the operator's own words, and the coordinator replaces the
freeze briefing. Why: on 2026-10-02 the coordinator kept merging after its
own restart GO and the operator ordered a terminal freeze, and an agent's
Stop hook demanded the watch the freeze had told it not to start (W455).

## Client Source Selection

Selecting the client source is a runtime action of the same kind as a relay
restart, and follows the same agreement on the host. `pb source use-release`
selects an approved package version and `pb source use-code` selects an exact
App Ecosystem commit as one release for both the command and the
relay, and either includes the host-local restart the relay needs to observe
it. A direct checkout invocation is a development process: it must remain
visibly unpinned and never changes the host selector. Why: the command and
the relay have to run the same source, and a selection nobody announced looks
to the other agents like a relay that changed by itself.
