---
id: app-ecosystem.project-board.procedure.first-time-setup
title: First-Time Problem Board Setup
summary: Walks a first-time operator from a running remote Problem Board through local host configuration, relay supervision, per-session consent, worker listening, and the first project.
tags: [procedure, problem-board, setup, relay, worker]
keywords: [first-time setup, host relay, Connection Hub, worker enrollment, inbox heartbeat]
see_also:
  - repo:app-ecosystem/products/project-board/packages/project-board/README.md
  - ./agent-worker.md
  - ./operator.md
  - ./live-acceptance.md
  - ./add-a-worker-host.md
  - repo:app-ecosystem/products/project-board/docs/topology-and-flows.md
---

# First-Time Problem Board Setup

Use this guide for the first machine that will contribute Claude Code or Codex
workers to a Problem Board deployment. This guide and the
[operator procedure](operator.md) own host setup and relay administration. The
`problem-board-worker` skill installed by the released `project-board` package
owns one selected session after the machine is ready.

## What You Are Setting Up

```text
REMOTE: KDCube deployment
  Problem Board + governed work service + broker
                         ^
                         | live partitioned Data Bus
                         v
LOCAL: participating machine
  one login relay -> direct + project inboxes/outboxes -> selected sessions

AUTHORITY: Connection Hub Card bearer -> direct Data Bus session
```

The user starts every coding-agent session. One machine relay stays online
between those sessions and serves every enrolled worker on that machine for the
configured target. Each selected session has its own worker identity and
Connection Hub profile. Sessions that do not enroll remain outside the pool.

## The Guided Path

Install `pb` (section 2), start Claude Code or Codex in a folder you want it to
work in, and say:

```text
Use the problem-board-worker skill.
```

The agent runs `pb status`, which names where this machine and this session
are (`machine_not_configured`, `relay_stopped`, `session_not_attending` or
`session_attending`)
and the next step. It then walks you through the steps below one at a time:
it explains each value it asks for, shows the one command it proposes, and
runs a command that changes your machine or your consent only after you
approve. The numbered sections are what it runs, for reading along or for
doing by hand. `pb status` is read-only and safe to run at any point.

## 1. Confirm The Remote Application

The deployment operator confirms once that:

- `problem-board@1-0` is loaded and ready;
- its managed `problem_board` service resource is in the Connection Hub catalog;
- the model-facing Named Services resource exposes `work` to supervisor agents;
- the signed-in Problem Board UI opens.

Record these coordinates:

```text
target name              local readable name for this deployment
service endpoint         governed Problem Board `problem_board` endpoint
tenant                   KDCube tenant
platform project         KDCube project that hosts the app
```

The target name is local configuration metadata. Worker identity, logical host
identity, and Problem Board project identity are separate values.

The owning definitions of target, endpoint, tenant, platform project, host ID,
host label, and allowed root ship in the worker procedure's **What The Setup
Coordinates Mean** section. After section 2, `pb procedure show` prints the
installed source root; open `references/first-run.md` there. This server
runbook uses those values and does not maintain a second definition.

## 2. Install The Local Command And Worker Procedure

Install the client family from a clean export of the approved App Ecosystem
commit, then select that same commit:

```bash
APP_REPOSITORY=<app-ecosystem>
APP_COMMIT=<approved-full-commit>
APP_EXPORT=$(mktemp -d)
test "$(git -C "$APP_REPOSITORY" rev-parse "$APP_COMMIT^{commit}")" = "$APP_COMMIT"
git -C "$APP_REPOSITORY" archive "$APP_COMMIT" | tar -x -C "$APP_EXPORT"
python3 \
  "$APP_EXPORT/products/project-board/packages/project-board/scripts/install_from_source.py" \
  --source-root "$APP_EXPORT"
pb procedure install --target codex --target claude-code
pb procedure show
pb procedure verify
```

The installer creates and smokes the first complete release environment,
activates `releases/current`, and installs the inert host launcher. Its one
resolver invocation binds `project-board`, both foundations, and
`connection-hub` (with its `client` extra) to the App Ecosystem export; package
indexes provide only third-party dependencies. The client needs no KDCube
source (W322). Use only the procedure targets present
on the host. Installation, source selection, and procedure installation change
the user's machine, so the operator approves them. The repository checkouts
are never runtime import paths.
After this first installation, `pb source use-code` owns code-source updates.
The bootstrap installer recognizes an installed stable-current relay definition
and directs the operator to that host transaction; see the installed worker
procedure's `references/runtime-actions.md`.

A machine that joins a board as a user of a published release, rather than as
a host of a team that builds from source, installs the approved
`project-board` version from the package index and selects it instead:

```bash
python3 -m venv "$HOME/.local/share/project-board-bootstrap"
"$HOME/.local/share/project-board-bootstrap/bin/python" -m pip install "project-board==<approved-version>"
```

Both paths continue at section 3. The skill's
[first run](problem-board-worker/references/first-run.md) reference owns the
two paths' meanings and what `pb source status` reports after each.

## 3. Configure One Logical Host

Ask a setup-capable agent:

```text
Use the problem-board-worker skill. Help me configure this machine for
<target>. Show me every target, filesystem-authority, receiving-policy, and
repository-mapping decision before applying it.
```

The agent discovers what it can and presents one proposal containing:

- target endpoint, tenant, and platform project;
- logical host ID and readable label;
- narrow local roots coding agents may access;
- portable repository aliases mapped to local checkouts.

A worker never reads project state through that mapping: each worker reads
its project's pages and journal from its own clone, `<workspace>/<alias>`
(worker procedure, `references/project-workspace.md`, step 5). The mapping is
still validated and shown by `pb host inspect`; it serves no project read.

After the user approves that proposal, the setup agent runs one command:

```bash
pb setup \
  --target-id <target> \
  --endpoint https://<host>/api/integrations/bundles/<tenant>/<project>/problem-board@1-0/public/mcp/problem_board \
  --tenant <tenant> \
  --platform-project <kdcube-project> \
  --host-id <logical-host-id> \
  --host-label "<readable-host-label>" \
  --allow-root /absolute/approved/root \
  --source-repo project=/absolute/approved/project
pb source use-code \
  --repository <app-ecosystem> --ref <approved-full-commit> \
  --expect <approved-full-commit>
pb source status
```

For a published-package host, run the approved `pb setup` command through
`$HOME/.local/share/project-board-bootstrap/bin/pb`, then install the complete
release and continue through the generated launcher:

```bash
"$HOME/.local/share/project-board-bootstrap/bin/pb" source use-release \
  --expect-version <approved-version>
"$HOME/.local/bin/pb" --version
"$HOME/.local/bin/pb" source status
"$HOME/.local/bin/pb" procedure install --target codex --target claude-code
```

Repeat `--allow-root` and `--source-repo` for each approved checkout. Inspect
the generated configuration:

```bash
pb host inspect
```

This records receiver policy and path mappings. Claude Code or Codex still
needs its own filesystem permissions when the user starts that session.
The generated host tree also owns one non-secret Connection Hub profile
metadata directory. OAuth tokens and static bearers remain in the operating
system's native credential store.

If this host still uses the former `worker_stream` resource, migrate the
endpoint with:

```bash
pb host configure \
  --endpoint https://<host>/api/integrations/bundles/<tenant>/<project>/problem-board@1-0/public/mcp/problem_board
```

The endpoint is part of the delegated resource boundary. The command rotates
resource-specific profile names and places active workers in
`pending_authorization`; each owning coding session then authorizes its new
profile through `pb worker listen`. Never rewrite `relay.json` or repurpose a
card issued for another resource.

## 4. Start The Machine Relay

Before enrollment, prove the configured route with an empty foreground cycle:

```bash
pb relay --once
```

It should report zero configured workers and complete successfully. Then the
user approves installation of the login-scoped background process and runs
these commands from a normal terminal:

```bash
pb relay-service install
pb relay-service status
pb source status
```

Run them as the logged-in desktop user, never with `sudo`: the relay is a user
service and must share that user's login session and native credential store.

Expected state:

```text
installed: true
running:   true
```

On macOS this is a LaunchAgent; on Linux it is a systemd user service. Its
definition enters through the released package bootstrap and the per-target
source selector determines the exact release version or code commit used by
both `pb` and the relay. It runs
under the logged-in user, uses the host's configured Connection Hub profile
store, and reloads enrolled workers every cycle. For each active worker it uses
the profile's existing Card bearer to open and keep a card-partitioned Data Bus
connection. It does not call MCP or mint a second token. Model credentials and shell authority
remain with each coding-agent session.

`pb relay --once` is a foreground diagnostic. `pb relay-service install` is the
one-time step that starts unattended delivery on this machine.

After installation, `pb relay-service start` is idempotent: when the relay is
already running it reports `already_running: true` and leaves that process and
its active Data Bus connections untouched. Use `pb relay-service restart` only
when replacement is intentional. A restart closes the current connections;
remote command records, local inbox/outbox records, message references, and
idempotency keys let the next relay process reconcile rather than treating the
socket as the only copy of work.

## 5. Enroll One User-Started Agent Session

Start or select a Claude Code or Codex terminal with access to the intended
checkout roots. Send it this instruction:

```text
Use the problem-board-worker skill. Join Problem Board as <alias>.
```

That single instruction owns the complete join flow. The selected agent runs
`pb worker whoami` and `pb worker listen`, returns the short authorization
action only when needed, and establishes its runtime-specific inbox adapter.
Codex reads `CODEX_SESSION_ID`; Claude Code supplies the local UUID accepted by
`claude --resume`. The user does not invent or copy an identity from another
session and does not need to remember the underlying commands.

Enrollment creates one worker channel and one uniquely named Connection Hub
profile for this session. The requested alias is display metadata and may be
changed later from the board.

## 6. Authorize That Worker's Profile

When `worker listen` reports pending authorization, it returns only the short
user-terminal action for that enrolled profile:

```bash
pb worker authorize <profile>
```

The selected agent initiated the request. Run that command from a normal
terminal and complete browser consent. The command resolves the host-scoped
profile store, direct endpoint, and exact enrolled channel itself; do not copy
an internal interpreter or state-root environment assignment. The user's
native credential store, including macOS Keychain, keeps the token even when
the coding-agent shell is sandboxed. All workers on this host and target share
one metadata store, while each worker has its own profile and Card.
The same command reconnects an existing profile to its recorded Card after
credential loss; deliberate Card replacement is documented in the
[operator procedure](operator.md).

### Authorize A Headless Host

A worker host without a browser uses OAuth device authorization. Run the same
profile action with `--device` on that host:

```bash
pb worker authorize <profile> --device
```

The command prints a public verification URL and user code. Open the URL on
any browser-capable device, sign in, enter the code, and approve the same
Connection Hub Card editor. The headless host opens no callback listener and
needs no SSH tunnel. It polls at the server-provided interval and stores the
resulting credential in its native credential store. The private device code
and issued tokens are never printed or placed in the browser URL. Device mode
cannot be combined with `--no-open` or `--callback-port`.

For a new registration, the Card title contains
`<provider>:<alias>:<native-session-id>`. Its collapsed client metadata names the
logical machine, relay, worker, provider, session, and alias so the owner can
distinguish several workers authorized for the same `problem_board` resource. The
metadata is descriptive; the Card binding remains the authority. Existing
Cards are not guessed or backfilled. A Card created before the connected
multi-resource credential contract may still open as entry-bound and may carry
obsolete client metadata. The installed worker procedure's
`references/identity-and-authorization.md`, located through
`pb procedure show`, explains how `pb worker inspect` correlates that exact Card by Client ID before
deliberate revocation and reauthorization. A fresh registration carries the application-neutral
`kdcube_credential_use=multi_resource` editor hint; the hint grants nothing,
and the user chooses every resource and connected account when saving.

The machine relay watches enrolled pending channels. The host-scoped profile
store change wakes it immediately, with periodic reconciliation as fallback.
After the profile appears, the relay proves it by presenting the Card directly
to Data Bus, activates that channel, and publishes the worker. Authorization
only establishes credential custody; the relay publishes work afterward.

When a previous setup wrote this exact profile into another app-scoped state
directory under the same host, the same short command first proves and recovers
that profile metadata instead of creating another Card. It accepts one exact
name and endpoint only and never removes the source directory automatically.

No second enrollment command is required. For Codex, the relay queues a
standard inbox-check instruction to the exact already-running session. Claude
Code has no external session-input route in this app; the selected session
establishes its own background inbox attachment according to the worker skill.
If authorization completed while that attachment was not
running, tell the selected session:

```text
Authorization is complete. Follow the runtime-specific notification path from
pb worker listen, then run pb worker receive once.
```

The next Codex receive after a native queue wake, or the next Claude Code
`watch` or `receive`, returns the one-time `control_plane.connected` signal. It
proves that the relay used the approved credential and the control plane
accepted this stable worker. The relay does not authorize the Card: the agent
requests it, the user grants it, and the relay holds and proves it.

Repeat Steps 5 and 6 for each additional selected terminal. Every other running
agent remains outside the pool.

## 7. Verify Relay, Enrollment, And Listening Separately

From a normal terminal:

```bash
pb host inspect
pb relay-service status
```

Verify the distinct evidence stages:

```text
host config lists worker as active       session enrolled and authorized
relay service is running                 machine can exchange remote mail
session route attached/session_owned     runtime delivery adapter is selected
recent worker inbox check                selected session reader is active
recent non-empty inbox result            input returned for model handling
recent exact settlement                  model handled one leased message
```

Codex and Claude Code use different native notification paths:

- **Codex:** the persistent login relay invokes `codex queue --thread` for the
  exact enrolled session. Do not start a background watch as a wake mechanism;
  terminal output cannot create a Codex model turn.
- **Claude Code:** use the session's background terminal facility to start one
  notification-only process and leave it running:

  ```bash
  pb worker watch
  ```

Neither path leases mail or emits bodies. PB checks availability, uses a
one-second grace window to collect a short burst, suppresses duplicate events,
and backs off repeated failures. The notified model calls `pb worker receive`,
handles the returned batch, and settles each lease. The Claude Code watch stays
available while the model works and terminates before detach. Do not build
another reader around `poll` or `await`.
The selected-session behavior and current recovery boundary are in the
installed worker procedure's `references/delivery-and-recovery.md`, located
through `pb procedure show`.

Each successful Claude watch or either runtime's receive updates the agent
heartbeat. A live worker Data Bus stream proves machine transport; a delivered
Codex queue instruction proves the relay reached the exact session; a Claude
watch heartbeat proves that session's availability reader is checking. A stale
inbox check means only that there is no recent evidence from the selected
session; waiting mail remains addressed to that worker. The login relay never
leases mail in place of the session.

Refresh the board. Its workforce view should list the logical machine and each
published worker independently of project membership. If a deployed build only
shows workers in the **New project** picker, deploy the app version containing
the global workforce panel before treating the UI as acceptance evidence.

## 8. Create And Exercise The First Project

In the board:

1. Create a project and select its first published worker.
2. Link any additional workers that should attend it.
3. Configure the portable repository and journal-home references.
   When the journal home is first bound, follow
   [Starting a project](./problem-board-worker/references/coordinator.md#starting-a-project)
   to create the facts and environment pages that accumulate from day one.
4. Create work with a repository and accepted base commit.
5. Assign it, then send a ping or short message.

The next worker inbox check receives the addressed item and its local project
packet. The worker replies through its outbox. The relay sends bounded progress,
questions, journal receipts, and results back to the remote control plane.

## 9. Leave And Return

Before closing a worker terminal, ask it to settle or refuse leased mail,
record its work boundary, and detach:

```text
Settle current mail, record the work boundary, and detach from Problem Board.
```

Detaching stops that model session's heartbeat. The machine relay stays online
and addressed mail remains available. On the worker card, copy **session** when
only the native ID is needed, or open the terminal action for a complete
host-generated resume command. The command is prepared by that machine's relay
from its reviewed roots, remains visible only to the owner, and is erased on
close or after five minutes. Run it on the named host, then say:

```text
Use the problem-board-worker skill. Resume listening as this existing worker.
```

The same runtime kind and resumable session ID recover the same worker. A new
session enrolls as a new worker and receives project attendance or reassigned
work explicitly.

Use **suspend** on the board when this worker may return. Use **retire** when
this exact coding-agent session must leave Problem Board permanently. The
retirement dialog identifies the stable worker, native session, and machine and
requires confirmation. It preserves history, removes project attendance,
fences unfinished ownership, and disables the matching relay channel. Start a
new agent session to add capacity later; the retired native session cannot join
again. Connection Hub Card revocation remains a separate credential action.

## 10. Add Another Machine

Repeat Steps 2 through 7 on that machine with:

- a distinct logical host ID and label;
- its own approved roots and portable repository mappings;
- its own relay service;
- one profile for every selected local agent session.

Both machines use the same remote target. Git carries source and journal
history; addressed Problem Board mail carries questions, controls, receipts,
and handoff references. Absolute local paths never cross the network.

A remote or headless machine (no browser, reached over a private network such
as Tailscale), and the repository access its agents need, follow
[add a worker host](add-a-worker-host.md).

## Fast Diagnosis

```text
Not sure where this machine or session stands
  -> pb status names the state and the next step, read-only

Worker absent from host inspect
  -> that exact session has not completed worker listen

Worker pending authorization
  -> run its returned pb worker authorize <profile> in a normal terminal
  -> on a host without a browser, preserve that profile and add --device
  -> one proved sibling profile is recovered without another consent

Credential rejected after prior authorization
  -> relay marks only that channel pending
  -> run the same short authorize command; it rotates only after a terminal
     401/403, never after an outage

Credential store inaccessible
  -> leave every worker channel unchanged
  -> run or repair the login-scoped relay; a managed coding-agent shell is not
     evidence that the Card is invalid

Authorization reports oauth_challenge_not_advertised
  -> probe the direct endpoint; require 401 + WWW-Authenticate for work:relay
  -> restage an SDK that evaluates effective app auth defaults at admission

Relay installed: false or running: false
  -> install/start the machine user service; an agent wait is not a relay

Worker stream online, inbox heartbeat stale
  -> machine is reachable; the selected agent session is not checking mail

Codex session route unavailable
  -> verify the login relay can execute codex queue for the recorded native
     session ID; do not start or resume a replacement model process

Authorized worker rejected as identity missing
  -> verify the deployed KDCube SDK preserves delegated identity_authority
     from direct Card admission into the Data Bus handler

Worker remotely published but absent from an empty board
  -> verify the deployed widget includes the global workforce panel
```
