---
id: app-ecosystem.project-board.procedure.operator
title: Operate Problem Board And Its Local Relay
summary: Explains the host command installation, one-relay multi-worker boundary, Connection Hub authorization, receiving-machine policy, board evidence, and operator controls.
tags: [procedure, operator, relay, problem-board]
keywords: [Connection Hub profile, host relay, worker session, receiver policy, OAuth reconnect, replace Card]
see_also:
  - ./agent-worker.md
  - repo:app-ecosystem/docs/project-board/relay.template.json
  - ./live-acceptance.md
  - repo:app-ecosystem/docs/project-board/topology-and-flows.md
  - repo:app-ecosystem/docs/project-board/storage-and-retention.md
---

# Operate Problem Board And Its Local Relay

This runbook owns machine configuration, host command installation, relay
lifecycle, receiving policy, and operator-controlled authority. The user
retains the decisions that grant authority: target, filesystem roots,
repository mappings, Connection Hub consent, peer policy, project attendance,
assignments, suspension, and revocation. One selected coding-agent session uses
the `problem-board-worker` skill installed by `project-board` after the machine
is ready.

## Host Command Dependencies

Install the client family from clean exports of the approved App Ecosystem and
KDCube commits, select those commits, then install its worker procedure:

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
pb procedure install --target codex --target claude-code
pb procedure verify
"$HOME/.kdcube/client-runtime/tools/problem-board-venv/bin/python" -c 'import json; from project_board.client.source_control import installed_release_source; print(json.dumps(installed_release_source(), sort_keys=True))'
```

The source installer creates the isolated environment and guarded launcher,
and resolves all six first-party distributions from both exports in one pip
operation. Use only the coding runtime targets present on this host.
`app-foundation` owns the generic MCP and Data Bus clients, `connection-hub`
owns the host-neutral
Connection Hub contracts, `connection-hub-cli` owns profiles and native
credential custody, `kdcube-cli` supplies its KDCube management dependency,
`service-foundation` owns relay lifecycle and wakeable
waiting, and `project-board` owns the host protocol, local field, command,
relay, and worker procedure. This app owns the server surfaces and imports the
shared package contract.

The exact KDCube refresh invocation is owned by
[the maintainer rebuild procedure](repo:app-ecosystem/products/kdcube/procedures/maintainer-rebuild.md)
in this repository. Host procedures point there instead of maintaining another
copy.

## One Host, One Relay, Many Sessions

`pb setup` creates a target-scoped host config. Immediately after setup, select
the same reviewed commits that provided the bootstrap, then inspect both records:

```bash
pb source use-code \
  --repository <app-ecosystem> --ref <approved-full-commit> \
  --expect <approved-full-commit> \
  --kdcube-repository <kdcube> --kdcube-ref <approved-full-kdcube-commit> \
  --expect-kdcube <approved-full-kdcube-commit>
pb source status
pb host inspect
```

`config/relay.template.json` documents the generated shape and is not a file
the user should assemble by hand.

```text
one machine + one target
  one logical host
  one local field containing many projects
  one local journal workspace and private repo map
  one receiving-machine policy
  one relay process
    N worker channels
      one native coding-agent session identity
      one Connection Hub profile/credential for this target
      zero or one current project attendance
```

The relay reloads the host config each cycle, so a newly enrolled session does
not require another relay process. Each worker channel uses its Connection Hub
profile for the governed `problem_board` service resource and presents that
Card bearer directly to one Data Bus connection. No second token is minted.
That connection is
the server's relay-presence evidence. Reference-only push events wake a
reconciliation cycle immediately; `relay.reconcile_ceiling_seconds` and
`relay.idle_reconcile_ceiling_seconds` are the ceilings on that wait, the
longest the relay will go without a cycle when no push arrives, and they remain
missed-event fallbacks rather than a polling schedule. A relay.json still
carrying the older `poll_interval_seconds` spelling is read unchanged. A cycle in
which every channel fails on a transport condition backs off and retries. A
credential cannot change to a different runtime session, and a profile cannot
be shared by two local channels.

The all-channel retry stays inside the running relay process and uses the host
runtime's bounded backoff, including when the first channel open after a
platform refresh meets a temporary OAuth metadata rejection. A non-retryable
Card rejection exits with its exact error code; the persisted channel pacing
then prevents a rejected credential from becoming a restart loop. The worker
skill's first-run reconnect section owns the restart decisions for transient
and credential refusals.

A retryable authorization or metadata outage opens a durable, per-worker
`relay_diagnostic` in the machine-local field. `pb host inspect`, `pb
relay-service status`, and `pb worker list` show its exact code and start time
without depending on the unavailable route. The first later successful
authorized cycle closes the interval and offers its code, start, and end through
`worker.publish`. Publication is best-effort: recovery proceeds immediately,
and an unacknowledged interval stays local for a later cycle.

For a coordinated live acceptance window, the host operator can exercise this
path without changing a Card or credential:

```bash
pb host relay-fault inject \
  --worker <stable-worker-name> \
  --code oauth_metadata_request_failed \
  --confirm-live-interruption
```

The command accepts one active worker on this host and one allowlisted
retryable relay code. It writes an expiring machine-local switch and wakes the
existing relay. The relay consumes the switch atomically, drops only that
worker's current channel session, raises the selected failure once, and opens a
fresh Card-authorized session on its next cycle. With no armed switch, this
path does no work. The Card, profile metadata, and native credential remain
unchanged.

Inspect or disarm an unconsumed switch with:

```bash
pb host relay-fault status
pb host relay-fault clear --worker <stable-worker-name>
```

This host-local command is for coordinated acceptance testing. It is absent
from Problem Board's MCP, Named Services, Data Bus, and browser surfaces. Run
the degraded and recovery checks in `live-acceptance.md` immediately after
arming it.

The setup agent first proves one foreground cycle with `pb relay --once`, then
uses `pb relay-service install` and `pb relay-service status`. The generated
LaunchAgent or systemd user unit restarts the relay independently of coding-
agent terminals. Its definition and logs are reported in command output. Host-
boot service authority beyond the user's login session remains an explicit
administrator decision.

On macOS, install, start, and restart wait for the prior LaunchAgent to leave
launchd before loading the current definition. A transient launchd bootstrap
return code 5 is retried with a bounded backoff. A terminal service-manager
failure reports its command, return code, and stderr; source activation keeps
that evidence on `work_client_source_activation_failed` after restoring the
previous source. systemd service changes use its native start and restart
transactions.

The relay emits one structured warning when a cycle takes at least five
seconds. `total_seconds` measures the complete cycle; `stages` is bounded
JSON that names each stage, worker channel, operation, duration, and outcome.
Startup mailbox reconciliation is the `startup_recovery` stage. A coordinate
request that waited at least five seconds also reports its queue duration and
the bounded stage/channel/operation records that overlapped that wait. These
records contain controlled labels and timing evidence, never request payloads.
The supervised relay and the direct single-cycle command use the same timing
and logging rule.

The service definition sets the relay's file-descriptor limit
(`SoftResourceLimits.NumberOfFiles` in the LaunchAgent, `LimitNOFILE` in the
unit). Without it the relay inherits the login session's ceiling, 256 on a
default macOS host, and the relay has hit that ceiling once, under a
client-session leak, with the failures reported as connection errors. The
value and its derivation are on `RELAY_FILE_DESCRIPTOR_LIMIT` in
`project_board.client.relay_service`. A relay that is running keeps the limit it started
with: after upgrading to a release that carries the limit, run
`pb relay-service install` again so the definition is rewritten, then
`pb relay-service restart`. The relay logs `file_descriptor_limit=` on its
first line at start, which is how a log reader tells a run under the
inherited ceiling from a run under the rendered one. A failure under the
ceiling is reported as `work_relay_descriptor_exhausted`, not as a
connection failure.

The service definition always enters through the released `project-board`
bootstrap. One per-target selector then governs both ordinary `pb` commands
and the relay. `pb source use-release --expect-version <version>` records an
approved installed version. `pb source use-code` takes the App Ecosystem and
KDCube repository paths, refs, and full approved commits. It exports and
verifies `project-board`, both foundations, `connection-hub`, and
`connection-hub-cli` from App Ecosystem plus `kdcube-cli` from KDCube as one
release ID.
When the service is installed, either source action restarts it and succeeds
only when the new process's startup record names the selected version or
release; otherwise it restores the prior selector and relay source. A direct
checkout invocation is a separate development process, reports
`client.pinned: false`, and never changes this selector. `pb source status`
shows the bootstrap, selector, and running relay source separately.

The generated config stores profile names, never access tokens. Host values
are logical labels rather than raw hostnames, IP addresses, hardware IDs, or
machine fingerprints. Absolute local paths remain in this operator-private
file and never enter remote projections.

Each host config selects one app-scoped Connection Hub directory for non-secret
profile metadata. This keeps enrollment inspection inside the approved client-
runtime tree while native Keychain, Credential Manager, or Secret Service
custody continues to hold token values. `pb worker listen` returns a short
`pb worker authorize <profile>` action. The command resolves the selected host,
metadata directory, exact worker channel, and endpoint itself, so the operator
does not copy internal paths or environment assignments.

For an existing OAuth profile whose token is missing, refused, or no longer
refreshable, the same command runs browser reconnect with the profile's
recorded OAuth client. It accepts only the recorded Card id and reports
`Reconnected Card <id> (grants kept)`. The operator can deliberately revoke
that Card and register a new standard worker Card with:

```bash
pb worker authorize <profile> --replace-card
```

Use `--coordinator` with a first or replacement authorization to propose the
whole Problem Board operation catalog. It does not change an existing Card
during an ordinary reconnect. The descriptor-owned profile and narrowing
rules are defined in [Delegated Access
Cards](repo:app-ecosystem/docs/connection-hub/package/delegated-cards.md#descriptor-owned-authorization-profiles).

That destructive path reports both ids as `Replaced Card <old> with <new>`.
An outage or transient transport failure does not select either browser path.

`allowed_roots` is the user's approved Problem Board boundary and repository
map. It does not modify the sandbox or filesystem authority of a Claude Code or
Codex process. Before enrollment, the selected session verifies its actual
access; the setup agent prepares a runtime-specific launch or resume command if
the user must restart it with additional approved directories.

## Connection Hub Entry

For a deployment that already carries Problem Board rows inside
`connection-hub@1-0.config.connections`, first transfer ownership in one
reviewed descriptor revision:

- remove the `work:coordinate`, `work:journal:view`, `work:observe`, and
  `work:relay` capability rows from Connection Hub's base catalog;
- remove the complete
  `problem-board@1-0/public/mcp/problem_board` direct resource from that base;
- retain the shared `kdcube-services@1-0/public/mcp/named_services` resource and
  every sibling namespace, removing only its `work` namespace;
- preserve the same rows under Problem Board's `config.delegated_catalog`.

Apply that complete descriptor revision before reloading the app. The resource,
operation, and grant IDs stay the same, so this moves catalog ownership without
widening any existing Card. Removing only recently added operations leaves the
older duplicate resource and namespace owners in place.

1. Stage `problem-board@1-0` with its `config.delegated_catalog` declaration
   intact, then reload that app. Application lifecycle reconciliation assembles
   all app declarations and publishes one immutable deployment catalog.
2. Run `kdcube bundle catalog check --workdir <runtime-workdir>`. Exit `0`
   proves that descriptor authority and the catalog serving requests match.
   Any other result names every drift path or the apps that declared the same
   capability, resource, or named-service namespace. Fix the owning app
   descriptors and reload those apps; do not merge rows into Connection Hub.
3. Verify both the managed `problem_board` MCP adapter and its partitioned Data
   Bus handler.
4. A selected agent runs `pb worker listen`. The command returns its unique
   profile name and, when needed, `pb worker authorize <profile>`.
5. The user opens the normal Connection Hub card editor from the authorization
   flow. The requested Problem Board operations and `work:relay` are proposed
   for that session; the
   user may add any other resources, operations, and connected accounts that
   this connected client should use. Saving the card completes consent.
6. The host-scoped profile-store update wakes the relay. The relay proves the
   approved pending profile, opens the stream, activates the channel, and
   publishes it through the Data Bus. For Codex, the persistent login relay
   invokes the exact-session native queue. For Claude Code, the selected session
   establishes exactly one background attachment running `pb worker watch`.
   Either notification reports availability without leasing message bodies,
   and the model calls `pb worker receive` to lease and handle them.

The selected agent requests enrollment, the user grants consent, and the login
relay keeps credential custody and proves the route. The relay never grants
consent. A managed coding-agent shell never needs Keychain access. When the
exact service resource rejects a Card, the relay demotes only that channel. The
short authorize command may revoke and replace it after a terminal `401/403`;
an outage remains a retry and does not rotate credentials.

Cards created before the connected multi-resource credential contract are not
silently reclassified. They may still carry obsolete client metadata and may
open as entry-bound cards. Correlate the exact profile and Card with `pb worker
inspect`, revoke that Card in Connection Hub, wait for the relay to mark only
that channel pending, and run the returned `pb worker authorize <profile>`
command. The fresh registration carries `kdcube_credential_use=multi_resource`;
that application-neutral metadata selects the full card editor but grants nothing by
itself. The user chooses every added resource and account when saving.

An `oauth_profile_store_read_failed` or equivalent native-store access error
does not demote or rotate a worker channel. It means the process running the
relay lacks credential custody. Run or repair the login-scoped relay; do not
reinterpret a managed coding-agent shell's Keychain denial as a bad Card.

The board alias may be changed at any time. Its stable address remains derived
from runtime plus resumable session ID. The project owner links that worker to
one current project independently of enrollment. The owner unlinks it before
linking it to another project.

## Changing What A Worker Card May Do

The Card selects the Problem Board service resource, canonical operations
under that resource, and their required grants. The relay presents that same
Card directly to Data Bus. Opening the socket proves the Card and resource;
every operation is then checked separately against current Card and catalog
state.

Adding an operation to the app's `delegated_catalog` declaration does not add it
to existing Cards. Raising one operation's grant requirement affects that
operation, not unrelated operations or the Data Bus transport. Prepare a
permission change in this order:

```text
1. the grantor adds the capability to each live card in Connection Hub
2. confirm every card in use carries it
3. only then raise that canonical operation's grant requirement and reload
```

A Card may carry a capability that nothing yet demands, so step 1 is harmless.
If the requirement is raised first, that operation is denied until each Card
is updated. Core relay operations continue only if their own selections and
grants remain intact.

Two habits follow from that failure and both are cheap:

Back up a descriptor before editing it, and test the edit against a copy first.
The script used there produced YAML that would not parse on one of the two
files, and that was found on the copy rather than on the live deployment.

When a permission change is proposed, inspect both the resource operation
selection and its required grants. They answer different questions: which
operation the caller selected, and which capabilities that operation needs.

## Re-Authorizing A Revoked Agent Happens At That Agent's Own Console

A revoked agent is not reachable through the managed network, and it must not
be. It holds no card, so it is not authorized to receive board events, and
anything that reaches it after revocation is reaching it by some path the
revocation did not close.

So the instruction to re-authorize cannot be sent to it. Not by the operator
through the board, and not by a coordinating agent, because both of those
routes are exactly what the revocation removed. The operator goes to that
agent's own console and tells it there.

This was got wrong in practice on 2026-09-12, twice. A coordinator sent the
consent link to the operator through the board after its own card was revoked,
into the one channel the revocation had closed, and the operator never received
it. Then the same coordinator told another agent to watch for its channel going
pending and re-authorize when it did, which that agent could not have observed,
because observing the board is the thing it had just lost.

It happened to work only because all three agents shared one machine and
same-machine mail travels through the local field rather than the control
plane. On separate machines nothing would have arrived. Do not build on that:
the local path is an implementation detail of a single host, not a channel a
revoked agent is entitled to.

The order that works:

```text
1. operator revokes the card
2. operator goes to that agent's console and tells it to re-authorize
3. that session runs the short authorize action and prints a consent link
4. operator opens the link and approves
5. the relay proves the new profile and the channel returns
```

A relay that reports online says the machine is reachable. It says nothing
about whether any particular agent on it is authorized. An agent whose card was
revoked sits on a perfectly healthy relay, which is why a channel can look
online while being unable to do anything at all.

## Migrating A Pre-Contract Worker Card

A Problem Board worker holds an application-neutral, multi-resource credential
so its card can gain resources later. Cards created before that contract open
as entry-bound: they behave like a card for a single-service MCP client and can
only vary tools within the one service they were made for. Such a card cannot
be given a capability for another resource, so it cannot be brought forward by
editing, and an operator sees workers grouped inconsistently for no reason they
can act on.

Correlate by client id rather than by label. The owning session runs
`pb worker inspect` and its `authorization.client_id` matches the Client ID
shown on the card, without reading the credential. Revoking by label alone
risks revoking the wrong card.

Migrate one worker at a time, starting with the least critical, so a failure
costs one agent rather than the pool. Revoke that exact card, let the relay
mark only that worker pending, then have the owning session run the short
`pb worker authorize <profile>` action and complete the browser consent. The
fresh registration carries the multi-resource hint, which grants nothing and
leaves every resource choice to the user.

## Receiving-Machine Policy

Remote project authorization and local receiving policy both apply. Update the
host policy through `pb host configure` after showing the proposed change to
the user.

`receiver_policy.allowed_control_kinds` bounds command kinds accepted on this
machine. `allowed_peer_workers` applies to cross-machine mail; an empty list
denies peers and `"*"` admits any session-bound worker already authorized by
the remote project. `max_control_bytes` is checked before a local mail write.
The receiving machine can always refuse an admitted remote command.

`receiver_policy.allow_session_resume_view` separately controls whether the
owner may ask this host to prepare a resume command. It defaults to `true` and
can be changed explicitly:

```bash
pb host configure --no-allow-session-resume-view
pb host configure --allow-session-resume-view
```

The relay builds command text from the exact runtime/session, the enrollment
working directory when it is inside an approved root, and the host's reviewed
roots. It never executes that command, selects a model, or adds a
permission-bypass option.

## What The Board Proves

The UI shows a mutable alias together with the stable session address and
runtime. Its status evidence remains separate:

| Evidence | What it proves |
| --- | --- |
| Worker stream online | This worker's card-partitioned Data Bus connection is live. |
| Control acknowledged | The relay wrote the command to the worker's machine-local inbox. |
| Session route attached | The login relay owns the exact Codex native-queue route; this label alone does not prove a model handled input. |
| Session route session-owned | The selected Claude Code session owns one background `pb worker watch`; this label alone does not prove it is alive. |
| `last_inbox_check_at` | A Claude watch or either runtime's receive operation checked the inbox. |
| `last_inbox_result_at` | A model-owned receive returned mail or a lifecycle signal. |
| Control seen | That receive named the exact control ref. |
| `last_mail_settled_at` | The session settled the exact local message lease. |
| Relay degraded interval | The worker's governed route recovered after the recorded error code and start/end times. |
| Assignment report | The current owner reported with the exact ownership version. |

Use ping to test the route. A relay acknowledgement without a later inbox check
means the machine is online but there is no later evidence that the model
session inspected its inbox. The message remains waiting for that worker.

## Attendance, Stop, And Authority

`project.link_worker` and `project.unlink_worker` change the worker's current
project attendance. Enrollment remains, while a link to a different project is
refused until the current project is unlinked. The attendance, matching access,
and retained membership event commit in one transaction. The PB service pushes
the changed refs to the worker stream; the relay then reconciles the current
attendance. The persistent login relay queues a standard exact-session Codex
instruction when matching local mail is pending. A selected Claude Code session
keeps one background notification-only `worker watch`; the model calls `worker
receive` after its compact event. A runtime without a supported native
notification adapter uses one-shot receives at safe boundaries and must be
reported as degraded because it cannot guarantee prompt attention between
model turns.

The selected Claude Code session terminates its background inbox attachment
before running `worker detach`; Codex has no session-owned watch to terminate.
Detach marks the agent session as
detached while retaining its relay channel and inbox for resume. Stopping the
relay removes its live stream presence. Moving a worker to limbo suspends
effective Problem Board operations. Revoking its Connection Hub Card rejects
the next incoming operation and removes its route from outbound live delivery.
A cooperative
stop message, detach, project removal, pool suspension, worker retirement, and
credential revocation are distinct operations.

Retirement is the permanent Problem Board action for one exact runtime and
native session identity. The owner opens it from the worker card and confirms
the stable worker shown with its machine and session. The control plane removes
that worker's attendance, withdraws controls that have not completed relay
settlement, advances unfinished assignment ownership, retains a tombstone and
history, and tells the matching host relay to disable only that channel. The
same session cannot enroll again. Retirement does not revoke the separate
Connection Hub Card; inspect or revoke that credential in Connection Hub when
transport authority should also end.

The Problem Board main scene includes a Connection Hub pane. Use the shield
action on any worker card to open that worker's exact delegated-access card in
that pane. The card address is non-secret; the bearer remains in native
credential custody. Resource, operation, connected-account, expiry, and
revocation decisions are made in Connection Hub. Worker retirement and card
revocation remain separate actions.

Discard is another distinct operation. The original sender may select several
earlier messages to one worker. Pending messages are marked discarded without
being delivered. If a message already reached the receiving session, its
history stays unchanged and that session receives one notice explaining which
requests are no longer wanted and how to handle work already underway.

Assignment reassignment increments an ownership version. Results from an old
worker or old version are rejected. Source collaboration uses the repository
and accepted base commit carried in the assignment; messages use the broker;
complete work and journals remain local or in Git.

The repeatable offline and live checks are in `testing.md` and
`live-acceptance.md`.


## The Inbox, Repository Links, And The Messaging Channel

Workers write to you with `pb worker send --recipient operator`. The board's
Inbox entry in the left rail lists that mail per worker with unread counts,
and each worker card in the pool shows its unread count. Opening a message
marks it read; the reply box under it sends one `reply` control to that
worker with the original message's correlation id, so the worker sees the
answer in its next inbox check as a reply to its own question.

Questions, blocks, and decisions also reach your linked messaging channel.
Two conditions: you linked your Telegram account to your KDCube account once
through the deployment bot's Mini App (the Connection Hub stores that link),
and the Problem Board app declares one enabled `telegram` integration row in
its descriptor whose `secret_refs.bot_token` points at the deployment bot's
Connection Hub authenticator secret, the same row shape the KDCube services
app carries. Without the row the inbox message shows `channel:
not_configured`; without the link, `not_connected`. Set the optional
`board_url` bundle property to the deployment's public origin. Each channel
notification then links to the public Problem Board site with the stable
worker address and exact inbox message ref; opening it selects that worker's
conversation and focuses the message.

Every message you send a worker is part of one conversation with that worker,
whatever its kind. The compact composer sits under the thread in the Inbox
view. **Direct conversation** works for every connected worker owned by the
signed-in user, including an idle worker with no project attendance. Selecting
that worker's current project adds its project context. Attendance changes
through explicit project controls and also forms the network in which project
workers may address one another.

The action menu contains message, reply, ping, replan, stop, and resume. A chat
message carries no manually typed work ref; work identity and ownership are set
through **Assign work**. Files can be sent with text or by themselves through
the Attach button, drop, or paste. **Message** on a pool card, **Message** on a
worker row inside a project, and **Request worker** on a project all open the
same thread. Message bodies use the platform Markdown renderer. Each card shows
one combined delivery state; the project timeline retains the separate relay
and session evidence for diagnosis.

The project view presents one paged timeline for controls, owner inbox turns,
and service events. Search and date, status, and worker filters run on the
server. Each row exposes its artifact URI; owner-worker rows open the exact
mailbox turn. [Storage and retention](repo:app-ecosystem/docs/project-board/storage-and-retention.md) defines
how that projection and conversation history continue across long projects.

Files travel with the same messages. In the Inbox thread, attach files to a
reply; each is scanned like a chat upload and stored on the reply's turn of
that worker's conversation, and the worker's relay fetches it by a signed
link into the worker's local field. A worker sends files with `pb worker
send --attach`; they appear on its message in the thread with the platform's
normal download link. Two requirements: the app's secret
`conversations.file_download_secret` in `bundles.secrets.yaml` signs upload
and download links (the same key the KDCube services app uses), and the
relay needs the public origin of the board, learned from your first board
request or set as the `public_base_url` bundle property. Without the secret
the reply box refuses attachments with `work_attachment_not_configured`.

Repository links on the board come from each machine's host mapping. Declare
the remote URL for a mapped alias with

```bash
pb host configure --set-source-repo-url applications=https://github.com/<org>/<repo>
```

The relay publishes alias-to-URL pairs with the worker registration; the
board renders a project's `repo:<alias>/<path>` journal ref as a link into
that repository. Local checkout paths never leave the machine.

## Resume Command From A Worker Card

The copy action beside **session** copies only the native session ID. The
terminal action requests a complete command from that worker's addressed host.
The owner sees a Connection-Hub-style command view and runs it in a terminal on
the named machine. This is useful after a user intentionally closed a selected
Claude Code or Codex process; the relay remains independent and does not start
the model session itself.

The request requires an active worker linked to the selected project. The
command exists remotely only in an owner-scoped five-minute view. Closing the
view erases it immediately; expiry performs the same erasure. If the receiving
host disables `allow_session_resume_view`, the view reports that refusal and
does not expose any path.
