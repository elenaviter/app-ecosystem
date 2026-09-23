---
id: app-ecosystem.project-board.procedure.live-acceptance
title: Run Problem Board Live Acceptance
summary: Verifies app-owned catalog publication, PostgreSQL plan authority and guarded local cutover, host setup, session-native enrollment and resume, direct delegated-Card admission, canonical operations over MCP and Data Bus, runtime-specific inbox checks, ownership fencing, receiver policy, and live revocation.
tags: [procedure, live-testing, problem-board, local-worker]
keywords: [live acceptance, guarded plan cutover, PostgreSQL plan authority, delegated catalog check, session identity, session resume command, project isolation, Connection Hub card, relay]
see_also:
  - ./agent-worker.md
  - ./first-time-setup.md
  - ./operator.md
  - ./testing.md
  - https://github.com/kdcube/applications/blob/main/playground/domain-solution/apps/problem-board@1-0/docs/topology-and-flows.md
---

# Run Problem Board Live Acceptance

This procedure separates setup, runtime, relay, and model-attention proofs. Run
them in order and record each independently: a loaded bundle is not proof of
delivery, local delivery is not proof of remote authority, and a relay receipt
is not proof that a coding-agent session read the message.

## 1. Prove Agent-Assisted Host Setup

Give a clean setup session `procedures/first-time-setup.md` and
`procedures/operator.md`, then ask it to configure one disposable target. It
must install the persistent `pb` command and the worker skill package for both
runtimes, discover or ask for the target coordinate, propose allowed roots and
repository mappings, and wait for user confirmation before granting local
authority. It must not ask the user to write JSON.

Run `pb procedure verify`. Both `codex` and `claude-code` must report the same
current source revision and digest, `state: current`, no errors, and every
declared package file verified. On a disposable home, corrupt one installed
reference and require verification to fail closed before reinstalling it.

Run `pb setup`, then rerun it with the same values. Require the same logical
host and config path. `pb host inspect` must show one local field, one journal
workspace, one receiver policy, and no enrolled workers. Change one repository
mapping through `pb host configure` only after an explicit approval and verify
that a mapping outside all allowed roots is rejected.

Run `pb relay --once`, then install and inspect the persistent host process with
`pb relay-service install` and `pb relay-service status`. Leave it running for
the remaining stages. Starting a coding-agent session is always a separate user
action. On a disposable test home, inspect the generated LaunchAgent or systemd
user unit before loading it.

## 2. Prove Runtime Readiness

Stage `problem-board@1-0` and `problem-board-knowledge@1-0` from their local
paths. Preserve the Problem Board app's `config.delegated_catalog` block.

On the first deployment after moving from a Connection Hub-owned Problem Board
catalog, apply one reviewed descriptor revision before either reload:

```text
Connection Hub base removes:
  capabilities  work:coordinate, work:journal:view, work:observe, work:relay
  resource      */problem-board@1-0/public/mcp/problem_board*
  namespace     work from the shared */kdcube-services@1-0/.../named_services*

Problem Board config.delegated_catalog retains those same identities.
The shared named-services resource and every other namespace remain unchanged.
```

This is an ownership transfer. It changes no existing Card and grants no new
operation. Reload the two apps, then inspect the catalog serving requests:

```bash
WORKDIR=~/.kdcube/kdcube-runtime/<tenant>__<project>

kdcube bundle reload problem-board@1-0 --workdir "$WORKDIR"
kdcube bundle reload problem-board-knowledge@1-0 --workdir "$WORKDIR"
kdcube bundle catalog check --workdir "$WORKDIR"
```

The check must exit `0`. It compares the complete descriptor-assembled catalog
with the immutable catalog serving authorization requests. Exit `1` reports
every missing, extra, or changed path. An invalid declaration reports both app
owners when they claim the same capability, direct resource, or named-service
namespace. Resolve that ownership in the app descriptors and reload the owning
apps. Do not copy rows into `connection-hub@1-0`; Connection Hub supplies the
base catalog while each app owns its additions. The check is read-only and
never adds an operation to an existing Card.

```bash
kdcube bundle status problem-board@1-0 --workdir "$WORKDIR" --json --live
kdcube bundle status problem-board-knowledge@1-0 \
  --workdir "$WORKDIR" --json --live
```

Both results must report `live.status: ok`, `live.loaded: true`, and
`live.preparation.ready: true`. In `chat-proc` logs, require a successful
capability-index line for `provider=problem.board.control`, registration of
the `problem_board.command.v1` Data Bus handler, and the managed
`problem_board` MCP surface. A non-fatal index warning means supervisor schema
discovery is degraded and the test stops there.

Before creating a worker credential, send an unauthenticated MCP initialize
request to the `problem_board` endpoint. It must return `401` and a
`WWW-Authenticate` header whose resource metadata resolves to the exact
Problem Board endpoint and advertises `work:relay`. A `200` response means the
managed route policy is not active. A transient `503` during startup is not an
authorization verdict; wait for Connection Hub readiness and retry.

## 3. Prove Session Identity And Host Multiplexing

Use two user-started sessions on machine A and one on machine B. Only these
three terminals receive the worker skill. Existing terminals that were not
selected must remain absent from the pool. Before enrollment, inspect the
host's approved roots and prove that each
selected session's actual coding harness can access the checkouts it may use.
Changing the host config alone must not grant that access.

In each selected terminal, run `pb worker whoami` and `pb worker listen`.
Codex must use `CODEX_SESSION_ID`; Claude Code must use the local UUID accepted
by `claude --resume`, never a `session_...` attribution ID. Give each returned
Connection Hub profile its own credential with the short
`pb worker authorize <profile>` command. Have each Claude Code session
establish its one session-owned background inbox attachment; verify that the
persistent login relay owns each Codex channel's exact-session native queue.
Require:

- three distinct stable worker addresses derived from runtime plus native
  session ID;
- one credential/profile per worker channel;
- each relay changes its approved channel from `pending_authorization` to
  `active`, and that session receives one `control_plane.connected` signal
  without rerunning `worker listen`;
- each selected Claude Code session runs one `pb worker watch`, routes its
  compact availability event into the exact model session, and never leases
  mail;
- the login relay queues a standard inbox-check instruction into each exact
  Codex session, and the model leases the real message with `pb worker
  receive`; neither runtime's notification path puts task bodies into model
  input;
- one relay process on machine A serving both of its channels;
- mutable aliases that can be renamed without changing addresses, credentials,
  attendance, or assignment ownership.

Link machine-A worker 1 to project A, machine-A worker 2 to B, and machine-B
worker 3 to A. A single `pb worker receive` from worker 1 must check only its
direct and A shards; worker 2 must see only its direct and B shards. The host
relay must discover and pull each channel's optional current project without a
restart.

Attempt to link machine-A worker 1 to B while it still attends A. The operation
must return `work_worker_already_attends_project`, name A as current and B as
requested, and require unlinking first. The board must show A and no Link action
for B. Unlink it from A, link it to B, and then unlink B and relink A for the
remaining cross-machine checks. Its stable worker and credential coordinates
must remain unchanged, each relink must start a new attendance, and the project
timeline must retain the link/unlink sequence.

Detach worker 1 and resume the same native Codex or Claude session. Rerunning
`worker listen` must recover the same worker address, profile, attendance,
inbox, and pending leases. Starting a new terminal session must produce a new
worker that receives no project traffic until it is linked or assigned.

From the signed-in board, copy that worker's labeled native session ID. Then
open its terminal action and require this sequence:

1. the REMOTE board creates an owner-scoped pending view and sends only its ref
   to the addressed worker channel;
2. the LOCAL relay applies `allow_session_resume_view`, verifies the exact
   runtime and native session, and builds the command from the host's approved
   roots plus the recorded working directory;
3. the board shows the complete command only after the relay publishes it with
   the matching content hash;
4. copying does not execute the command, and the command contains no model
   choice, automatic approval, or permission-bypass option;
5. closing the view erases the command, hash, and byte count; a second view
   left open for five minutes is erased by expiry;
6. another project owner cannot read or close either view.

Disable `allow_session_resume_view` on the host and request the terminal action
again. The relay must publish an actionable refusal without writing the command
into the worker's model inbox. Re-enable the policy before continuing. Running
the copied command remains an explicit user action in a terminal on that host;
the relay never starts or resumes Claude Code or Codex.

## 4. Prove Local Work And Continuation

Use separate machine-local Problem Board workspace roots (internally, `local
shared field` roots) on machines A and B. Bind project A to portable
journal-home and project-artifact refs in Git repositories. Require
`worker context` to resolve those refs through each machine's private mapping
without persisting absolute paths remotely.

For a project that still has a legacy local active plan, pause plan writes and
legacy publication, prove the loaded catalog exposes `project.plan.import`,
`project.plan.index`, `project.plan.item`, and `plan.notes.list`, and run:

```bash
pb plan cutover-local \
  --project-ref work:project:<project-id> \
  --preview > <reviewed-preview>.json
```

Inspect every `reference_rewrites`, `active_assignment_bindings`, and
`assignment_outcomes` row, and require `unresolved_assignment_count: 0`. Then
require `cutover_readiness` to report a canonical local source, zero prepared
migrations, and zero pending or leased plan publications and assignment
reports. Require each binding's `item_identity_rewrites` to expose any
canonical URI move even when its assignment was rewritten to the same
destination. Then
commit the exact returned `cutover_content_hash`:

```bash
pb plan cutover-local \
  --project-ref work:project:<project-id> \
  --reviewed-preview <reviewed-preview>.json \
  --expected-cutover-hash <cutover-content-hash-from-preview> \
  --expected-plan-revision <current-postgresql-revision> \
  --idempotency-key <stable-retry-key> \
  --confirm-cutover
```

Require a changed preview hash or modified preview package to stop before
import. Capture the governed import payload and prove it equals the full
package and reference mapping in the reviewed file; a current-source rebuild
may detect drift but must never replace that payload. Require the result count
and item-key set to equal the complete local source in
both directions. Read every item and note through the governed operations, and
confirm the local `plans/` tree was removed only after those reads matched.
Then run `pb plan sync` twice: the first call exports PostgreSQL to Git and the
second reports no byte change. Finish with `pb plan drift` and require
`in_sync: true`. Use the complete current local plan as recovery input; retain
older Git exports and legacy outbox history as evidence.

Queue an assignment with repository and accepted base commit. After the
runtime-specific notification, the addressed worker's `worker receive` must
return the leased message plus one bounded project context and must resolve the
current work item through PostgreSQL. The worker verifies
ownership and base, uses an isolated branch/worktree, writes a front-matter
journal, sends a review
request, reports the exact assignment version, and settles the message once.
The reviewer reads the request from its own addressed shard, replies through
its outbox, journals, and settles once.

From the worker thread, send a file with no text. Require the attachment name
to become the readable subject, the same project context to remain selected,
and exactly one conversation turn and one addressed control to be recorded.
Sending a normal message from the conversation must not link the worker to a
new project or accept a manually typed work-item ref; those changes belong to
the explicit link and assignment actions.

On machine B, clone or pull the same mapped Git homes into different absolute
paths, rebuild the local journal links/index, search for the first worker's
summary, and read the selected portable ref. This proves continuation from Git
and portable mappings; do not copy the first machine's symlink or SQLite file.

## 5. Prove Delivery Evidence And Safety

An empty successful `worker watch --once` or `worker receive` must update
`last_inbox_check_at`; enrollment and queue delivery alone must not. Stop the
selected session's checks and miss three declared check intervals: the session
becomes stale while relay presence and the Codex queue route can remain current.
Queue a ping and require separate evidence for relay acknowledgement, session-
route delivery, inbox result, and exact agent settlement. Let a Codex worker sit
idle beyond 300 seconds, then send a new ping: the standard inbox-check
instruction must still enter the same running session without a renewed wait
process.

For Claude Code, prove the attachment contract separately:

- idle watch checks emit no model event;
- several messages arriving within the one-second grace window produce one
  availability event without refs or bodies;
- watch never leases mail and remains usable while the model handles an earlier
  receive;
- the model's next receive leases several already waiting messages together;
- mail left beyond the receive limit produces a later availability event;
- one repeated failure is reported, identical failures back off without an
  event flood, and recovery is reported once;
- terminating the attachment before detach makes the inbox check become stale,
  and resume plus `worker listen` establishes a new attachment.

The relay must not claim model attention before either runtime's session-owned
read evidence.

### Exercise one transient relay authorization failure

Coordinate a short interruption of one active worker. Record the relay PID,
the worker's non-secret Card address, and the initial empty or prior
`relay_diagnostic` history from `pb relay-service status` and `pb worker
inspect`. Arm one exact metadata failure:

```bash
pb host relay-fault inject \
  --worker <stable-worker-name> \
  --code oauth_metadata_request_failed \
  --confirm-live-interruption
```

The command wakes the running relay. As soon as the named channel reports
`state=degraded`, capture all three machine-local views:

```bash
pb host inspect
pb relay-service status
pb worker list
```

Each view must show the same `oauth_metadata_request_failed` code and non-empty
`started_at` for the named worker. `pb relay-service status` must retain the
original PID, sibling channels must remain ready, and `pb host relay-fault
status` must show no pending switch because the relay consumed it once.

Wait for the next successful cycle and run the same three inspection commands.
The named channel must be ready and retain exactly one new `recent` interval
with the same code, non-empty `started_at`, `ended_at`, and `published_at`.
The board's worker row must retain that interval after the local route has
recovered. Confirm that the worker's Card address is unchanged and that normal
mail delivery resumes. This proves the real login-scoped process, durable local
diagnostic, recovered Card-authorized publication, and sibling isolation in one
run.

Select several earlier outgoing controls and discard them together. Require
one result per original ref. A remotely pending message must never reach the
relay; a message in the LOCAL inbox must move to ignored. A message already
leased or handled must retain its original state and cause one direct
`discard.notice` with the sender's reason and explicit not-started,
in-progress, and already-completed handling intent. The model must receive and
settle that notice. Repeating the discard with the same idempotency key must
not create another logical request or notice.

Assign one work ref to worker 1 at ownership version 1, then reassign it to
worker 3 at version 2. Reject worker 1's late version-1 result and accept worker
3's version-2 report. Remove the sender from machine B's allowed peer list and
require refusal before local materialization, then restore policy.

Suspend worker 3 from the active pool, attempt one send to it, restore it, and
poll. The attempted message must remain under `mail/ignored` with the internal
disposition `ignored_recipient_limbo`; restoration must not replay it. A
message created after restoration must be delivered normally.

## 6. Prove Card Admission And Canonical Operations

Each selected session has one Connection Hub Card for the
`problem-board@1-0/.../mcp/problem_board` resource. Select only the operations
that agent needs, then grant the corresponding `work:observe`,
`work:coordinate`, or `work:relay` capability. Add `work:journal:view` only to
Cards that may serve requested journal catalogs and bodies.

Prove the boundary in this order:

1. Inspect the authorized host profile and record its non-secret `access_id`.
   Confirm the bearer remains only in native credential custody.
2. Start the relay. Its Socket.IO handshake presents that existing Card bearer
   directly with tenant, project, bundle, and the exact `problem_board`
   resource. It must not call an MCP bootstrap tool or receive a second token.
3. The board reports the worker stream online only after the socket room and
   server subscription are ready. Redis routing/session records contain the
   Card address and selected resource, never the bearer.
4. Every operation below is a Problem Board service action. The MCP tool name
   and a `data_bus.publish` package's `payload.operation` on subject
   `problem_board.command.v1` carry that same canonical ID:

```text
work:observe
  project.control.get          journal.view.request
  journal.view.close

work:coordinate
  project.register             project.set_journal_home
  project.control.initialize   project.control.update
  project.link_worker          project.unlink_worker
  worker.rename                worker.evict
  worker.restore               worker.retire
  control.enqueue              control.discard
  assignment.assign

work:relay
  worker.publish               worker.heartbeat
  control.pull                 control.acknowledge
  control.refuse               control.discard_complete
  control.worker_settle        mail.route
  assignment.report            projection.publish
  event.publish                note.view.publish
  note.view.fail               project.report.publish
  project.report.fail          session.resume.publish
  session.resume.fail          attachment.request_upload

work:relay + work:journal:view
  journal.view.publish         journal.view.fail
```

5. List the managed `problem_board` MCP tools and confirm the complete catalog
   above is exposed as tool names. Invoke one permitted operation through MCP
   and the same operation through Data Bus. Both must enter the same live
   Card/resource/operation/grant guard and shared service handler; neither
   adapter may translate to a transport-owned ID. Invoke an overlapping action
   through Named Services and confirm it reaches that same handler under the
   named-services gate.
6. For one Data Bus operation, retain both receipts. The immediate ingress response
   must name the accepted `message_id`; the later `kdcube.data_bus.result`,
   `.conflict`, or `.error` must carry the same ID and say what the PB handler
   did. A timeout after ingress acceptance is `outcome_unknown`; retry with
   the same message ID and idempotency key.
7. Send a command from the signed-in board after the relay is waiting. The
   control row must commit first, then a `problem_board.worker.event.v1`
   packet containing only kind and refs must wake the addressed card
   partition. The relay pulls, materializes, and settles through the existing
   Data Bus session.
8. A relay-only Card may use selected relay operations but receives
   `work_worker_stream_grant_required` for a journal-view publish. A card with
   `work:journal:view` may publish only the explicitly requested view.
9. Remove one selected operation from the live Card while leaving its resource
   and other operations intact. That operation must return
   `work_worker_operation_not_granted`; another selected operation must still
   succeed on the same connection. Restore the operation before continuing.

Use one additional worker profile on a Linux host where no browser opener or
loopback callback listener is available. Run:

```bash
pb worker authorize <profile> --device
```

Require the authorization handoff to print only the public verification URL
and user code. Open that URL on another device, complete platform login and the
normal Connection Hub Card editor, and require the waiting CLI to honor the
advertised polling interval and finish without an SSH tunnel. The first
successful authorization must create exactly one Card and one local profile.
Remove the local credential while leaving that Card active, run the same
command again, and require reconnect to preserve the same `access_id` and
grants unless the user edits them. Exercise denial, expiry,
too-fast polling, excessive invalid user-code guesses, wrong client, Card
mismatch, Card revision conflict, and a second poller. Each must produce its
distinct bounded error; exactly one poller may consume approval, and consumed
or expired requests must never mint. Inspect CLI output, browser URLs, the
short-lived device record, and ordinary logs: none may contain an access token,
refresh token, or private device code.

Before project attendance exists, relay publication must place the worker in
the pool and return `attendance: waiting_for_link`. In the signed-in board,
link the worker and queue a fenced assignment. Require the relay to reconcile
the portable journal/source binding, apply receiver policy, materialize
atomically, and acknowledge only afterward. Require the selected session's
next `worker receive`, following a Claude Code `worker watch` event or a Codex
native queue notification, and then settlement before the board claims seen or
handled.

Send worker-to-worker mail between machines and verify route admission,
destination receiver policy, model read evidence, reply, and settlement. Link
another project while the relay is running and verify it is discovered. Unlink
only project A and verify A pulls fail while project B remains available.

Finally revoke one worker Card while its Data Bus socket is still open. The
next incoming operation must be rejected as `delegated_card_not_active`, and
an addressed outbound wake must not use that Card's registered route. The relay
must drop only that channel and mark it `pending_authorization`; sibling
channels on the same machine remain connected. No token-expiry waiting period
is allowed. Run the same short `pb worker authorize <profile>` command from a
normal terminal, complete new consent, and prove the channel reactivates
without another `worker listen`. Simulate an authority-store outage separately:
incoming work and outbound delivery fail closed for that attempt, the route is
retained for retry, and no credential is rotated.

As a separate negative case, invoke credential inspection from a process that
cannot read the native store. It must report `credential_store_inaccessible`
without demoting the active channel or rotating its Card. Restore or restart
the login relay and prove the existing channel reconnects unchanged.

The card grant and revocation are user decisions. An agent may prepare the
commands and open the consent flow, but it does not mint or recover a user
credential on the user's behalf.

## 7. Prove Permanent Worker Retirement

Use a disposable enrolled session after its delivery tests are complete. From
the board, open **Retire**, verify the stable worker, native session, and
machine, then confirm. Prove all of the following:

1. The worker disappears from the active pool and every project network.
2. Pending or relay-leased controls become withdrawn, and unfinished
   assignments advance their ownership version and close as worker-retired.
3. Another active worker on the same host receives the retirement tombstone;
   the relay disables only the matching local channel and detaches its listener.
4. With no active sibling, the retired channel's next publish is rejected and
   produces the same local disablement.
5. Repeating `worker listen` from the retired native session cannot restore,
   rename, or rebind it. A new native session joins as a distinct worker.
6. Historical conversations, control settlements, project events, journal
   refs, and the local mailbox remain attributable to the retired worker.
7. The Connection Hub Card remains visible until the owner separately revokes
   it; revoking it does not change the retained Problem Board tombstone.

## Evidence To Retain

Record installer and config paths, bundle generations, provider-index and Data
Bus-handler readiness, synthetic runtime/session kinds, stable worker refs,
aliases, current attendance and membership-event refs,
control/message/assignment refs, ownership versions, relay heartbeats, stream
presence, session-route state,
`last_inbox_check_at`, `last_inbox_result_at`, `last_mail_settled_at`, portable journal
refs, binding revision, cross-machine search result, ingress and handler
receipts, push latency, settlement outcomes, receiver-policy refusal, suspended
worker disposition, retirement tombstone and channel disablement, card grant
names, non-secret Card address, selected resource and operations, direct-bearer
admission without the bearer value, revocation outcome, authenticated client
versions, and browser results. Never record a
bearer, cookie, real native session ID, raw machine identity, absolute private
path, full project body, or private source content.
