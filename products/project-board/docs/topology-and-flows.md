---
id: project-board-topology-and-flows
title: Problem Board Topology And Flows
summary: The host, relay and worker view of Problem Board as ASCII flows, from the remote control plane and the linked Connection Hub Card through the per-machine relay to session-bound workers, mail, heartbeats, local work, Git handoff and authority changes.
tags: [project-board, architecture, topology, data-flow, multi-machine]
keywords: [control plane, broker, host relay, worker mailbox, project inbox, artifact timeline, worker heartbeat, ownership version]
see_also:
  - ./README.md
  - ./storage-and-retention.md
  - ./add-a-machine.md
  - repo:app-ecosystem/products/project-board/packages/project-board/src/project_board/procedures/agent-worker.md
  - repo:app-ecosystem/products/project-board/packages/project-board/src/project_board/procedures/operator.md
  - repo:app-ecosystem/products/project-board/packages/project-board/src/project_board/procedures/live-acceptance.md
---

# Problem Board Topology And Flows

This page is the host, relay and worker view of Problem Board: what runs on
each machine, how an addressed command reaches one coding-agent session and
how its reply returns. The application that serves the board owns the
service-side design (its tables, its user interface, its report ledger) in its
own documentation. What an operator of a worker host and an enrolled worker
need is kept here, with the client that implements it.

## Complete Topology

```text
REMOTE: KDCube deployment

  user in board UI                 agent supervisor
          |                         Named Services `work`
          +-------------------+-------------+
                              v
                 +-------------------------+
                 | Problem Board control   |
                 | projects + attendance   |
                 | assignments + versions  |
                 | broker + receipts       |
                 +------------+------------+
                              |
                   commit, then push refs
                              |
                 +------------v------------+
                 | PB worker Data Bus      |
                 | card partition + actor  |
                 | operation + result      |
                 +------------+------------+
                              ^
                              |
                 live bundle-scoped session
                              |
  Connection Hub worker Card  |
       bearer + PB resource ---+  direct admission; no token exchange
                              |
            +-----------------+----------------+
            |                                  |
            v                                  v
LOCAL: machine A                    LOCAL: machine B
+-------------------------------+  +-------------------------------+
| one target-scoped relay       |  | one target-scoped relay       |
|  worker channel A1            |  |  worker channel B1            |
|  worker channel A2            |  |  worker channel B2            |
|       | wake adapter |        |  |       | wake adapter |        |
| direct + project inbox/outbox |  | direct + project inbox/outbox |
|       |              ^        |  |       |              ^        |
| watch/receive or queue        |  | watch/receive or queue        |
|       v              |        |  |       v              |        |
| user-started coding agents    |  | user-started coding agents    |
|       |                       |  |       |                       |
| mapped Git repos + journals   |  | mapped Git repos + journals   |
+-------------------------------+  +-------------------------------+
```

KDCube stores the authoritative active plan, coordination state, immutable work
body refs, and owner-worker conversation turns. Each machine stores direct and
project mail, bounded working context, source state, and private path mappings.
Git transports accepted source, journal history, and exported plan revisions
between machines; the broker transports addressed commands and replies.

Before a worker changes shared Git, source, relay, or runtime state, it reads
and publishes through the Redis-backed shared-write dashboard, one expiring
status per project worker. It also publishes when in-progress source is not
ready for reload or restart. That project-wide status is separate from
addressed mail and from the bounded turn used to perform the change.

The browser API, model-facing Named Services, managed `problem_board` MCP, and
worker Data Bus are entrances to the same PB control service. The relay
presents its Card directly to Data Bus. MCP and Data Bus pass the same
service-owned canonical operation IDs to one live-Card guard.

## App Entry And Worker Opt-In

```text
user chooses a setup session
  -> reads first-time-setup.md + operator.md from this package
  -> pb setup
       target + tenant/project + logical host
       governed PB `problem_board` service endpoint
       approved local roots + repository aliases
       one machine-local Problem Board workspace + one relay configuration
  -> pb relay --once
  -> user may approve pb relay-service install

user chooses one existing Claude Code or Codex terminal
  -> says: Use the problem-board-worker skill.
           Join Problem Board as <alias>.
  -> installed problem-board-worker/SKILL.md owns this selected session
  -> pb worker whoami
       provider/runtime + native resumable session id
  -> pb worker listen
       one stable worker address
       one relay channel
       one deterministic Connection Hub profile for this session
       local control-plane state = pending_authorization
       one runtime-specific notification adapter
  -> selected agent returns pb worker authorize <profile>
  -> user runs that short command in a normal terminal and approves consent
  -> Connection Hub stores that profile's credential in native custody
       local profile-store change wakes the machine relay
  -> machine relay proves that the pending profile is now usable
  -> relay presents its Card bearer + exact service resource to Data Bus
  -> relay connects the Card partition and publishes the stable worker
  -> successful publication changes the relay channel to active
       local control-plane state = published
  -> runtime-specific session delivery
       Codex: login relay invokes codex queue --thread <same-session-id>
              with a standard inbox-check instruction
       Claude Code: selected session owns one background attachment
                    running pb worker watch
  -> that model's next inbox check receives control_plane.connected

every other terminal
  -> does not enter this procedure
  -> is not registered, polled, addressed, or shown in the pool
```

The worker alias is display metadata and may change. The stable identity is the
coding-agent provider/runtime plus its native resumable session ID. A worker
attends zero or one current project after enrollment. The owner unlinks it
before linking it to another project.

## Command Delivery And Reply

```text
DIRECT OWNER <-> WORKER

owner chooses any connected worker
  -> direct request / reply / ping, project_ref empty
  -> broker addresses that worker's direct lane
  -> relay writes the control to the machine-local direct worker inbox
  -> selected agent session checks that inbox and settles the message

worker sends to operator with project_ref empty
  -> LOCAL direct outbox -> relay -> worker handler
  -> owner's worker conversation + inbox

No project attendance is required for either direction.

PROJECT-CONTEXT FORWARD

user or supervisor
  -> choose an attended project + stable worker + command
  -> authorize project membership and requested operation
  -> broker records pending control
  -> PB emits a reference-only wake event to that worker-card partition
  -> addressed relay sends control.pull through the live Data Bus
  -> Data Bus ingress acknowledges package acceptance
  -> PB handler checks card, selected grants, partition, operation, and project
  -> broker leases exact control to exact relay/worker/project
  -> correlated handler result returns the lease and bounded command
  -> receiving-machine policy admits or refuses it
  -> admitted control is written atomically to that project inbox
  -> relay settles local delivery through the same Data Bus session
  -> PB handler result confirms application or denial
  -> relay detects pending local mail but does not lease it
  -> runtime adapter notifies the exact session without leasing or returning
     bodies: Codex native queue or Claude Code background watch
  -> model runs pb worker receive
  -> command and one bounded local project-context packet enter that model's context

RETURN

worker question / progress / result / journal receipt
  -> project outbox
  -> machine relay
  -> card-partitioned Data Bus operation
  -> PB worker handler -> Problem Board control plane
  -> supervisor or user

worker-to-worker message on the same machine
  -> resolve an attended worker in the same machine-local PB workspace
  -> receiving policy
  -> destination project inbox
  -> destination worker's next inbox check

worker-to-worker message on another machine
  -> sender outbox
  -> sender relay + Data Bus handler + control-plane address resolution
  -> broker
  -> reference-only wake on destination card partition
  -> destination relay pulls + applies receiving policy
  -> destination project inbox
  -> destination worker's next inbox check
```

Attendance is the project collaboration boundary. It supplies project source,
journal, assignment, and role context and permits project workers to address
one another. The owner-worker conversation exists independently and may carry
an optional project context on any message.

Delivery to a machine and observation by a model are separate events. A relay
acknowledgement never claims that the coding agent read the command.

A malformed message is isolated in quarantine instead of stopping the inbox,
a receive is transactional, a wake is acknowledged by the receive that names
it, and end-to-end reachability leaves evidence on both sides. The worker
skill's delivery-and-recovery reference names each state and its recovery.

The ingress acknowledgement means only that KDCube accepted the package into
the bundle stream. A later correlated result records what the PB handler did.
If that result is not observed before timeout, the outcome is unknown; a retry
uses the same message and idempotency identity. A slow reconciliation cycle
backs up missed wake events, while ordinary work is push-driven.

Data Bus acceptance and terminal-result envelopes are correlated transport
receipts. Only a receipt whose `message_id` matches this client's in-flight
request resolves that request. The client consumes another peer's receipt as
transport evidence and reserves the application-event queue for Problem Board
domain events, which wake reconciliation.

One worker channel owns one Data Bus client at a time. Before the relay opens a
replacement, it awaits shutdown of the superseded client, including any
Socket.IO reconnect task. A failed shutdown leaves that channel in a closing
state and blocks replacement in that process; the service supervisor replaces
the process so process exit releases every retained task. Relay and Data Bus
lifecycle records carry the stable worker name, worker identity as the channel
identity, and a monotonic replacement epoch. The epoch orders different client
objects even though each object's connection generation starts again at one.
Channel health is therefore established from the active channel identities;
historical refusal-line counts are diagnostic evidence, not a health metric.

## Relay And Agent Heartbeats

```text
machine relay
  -> card-scoped Data Bus socket is registered for this bundle
  -> proves this worker channel is connected to the remote app
  -> discovery heartbeat bootstraps attendance or repairs a stale link
  -> one periodic project heartbeat reconciles registration, attendance,
     active durable assignments, and coding-session inbox evidence;
     its fallback cadence comes from the listener's check_interval_seconds,
     bounded by the active reconciliation ceiling; it is not the connection signal
  -> sends a bounded session projection on the first cycle and after a change;
     an unchanged cycle omits agent_sessions
  -> a missing assignment control is repaired from the durable assignment
     with one notice keyed by stable assignment ID + ownership version
  -> first successful publication after consent records local state=published
  -> when local mail is pending, selects the recorded runtime adapter
       Codex: queues a standard inbox-check instruction to the exact session
       Claude Code: records session_owned; session attachment owns inbox reads
  -> never leases project mail for the model

user-started agent session
  -> Codex receives the standard exact-session instruction from the login relay
     or Claude Code keeps one pb worker watch in its background facility
  -> each durable local-mail write sends a best-effort notification to that
     Claude Code worker's own Unix datagram endpoint
       the watch binds before its first probe, so mail written before either
       relay or watch restart is found by that probe
       a missing, stale, or full endpoint loses no mail; the ordinary check
       interval remains the slow safety probe
  -> the selected adapter checks availability across direct + attended project inboxes
       no lease, no body, no retained delivery batch
       PB coalesces a short burst and suppresses duplicate notifications
       idle output is silent; repeated failures back off
  -> records last_inbox_check_at as listener-liveness evidence
     this routine record update does not wake the machine relay
     a Codex acknowledgement sets queue_reconciliation_required and does wake it
  -> compact event reaches the exact model session
  -> model calls pb worker receive
  -> receive leases available messages and records last_inbox_result_at
  -> exact settlement records last_mail_settled_at

operator ping
  -> relay acknowledgement      proves arrival on the machine
  -> watch event                proves availability reached the selected session
  -> optional Codex route       proves the additional instruction reached its queue
  -> next receive               returns the message to the model
  -> handled/refused settlement proves what the agent did with the ping
```

```text
stream live + inbox check recent = route online; agent checked recently
stream live + inbox check stale  = route online; no recent agent check
stream absent + check recent     = local agent recently checked; route impaired
stream absent + check stale      = no current evidence of either process
```

Problem Board never starts or schedules a subscription-backed coding agent.
The user starts the session. The selected runtime determines one notification
adapter: the persistent login relay owns Codex's exact-session native queue,
while a Claude Code session owns its background inbox attachment. A runtime
without a supported adapter uses one-shot `worker receive` calls and has no
concurrent notification path. The OAuth callback does not publish a worker or
claim model observation. The connection signal exists only after the machine
relay used the approved card, connected to the worker partition, and the PB
handler accepted publication. The local profile-store update is only a wake
hint; periodic reconciliation still covers a missed local notification.

An omitted `agent_sessions` field preserves the last remote projection;
an explicit empty list marks the prior sessions stale. The projection carries
current presence plus at most 20 recently observed control references. Native
wake histories remain in the machine-local field.

### Why the two runtimes wake differently

The two runtimes cannot share one wake, because each admits a new model turn
from one direction only, and the turn is what has to be created:

| | Codex | Claude Code |
| --- | --- | --- |
| **What starts a turn** | its own native queue: the host relay runs `codex queue --thread <session>` with an inbox-check instruction | output of the background attachment the session owns (`pb worker watch`), which the runtime surfaces |
| **Who owns the wake** | the host relay, one process per host, outside the agent's sandbox | the session itself |
| **Why not the other way** | inside its sandbox a Codex session cannot change its queue, and a watcher it starts can report that mail waits but cannot create a turn | Claude Code has no command that puts a message into a running interactive session, so no relay can push into it |
| **What the card shows between wakes** | "waiting for wake" while its relay reports; "wake relay stale" when the relay stops reporting | the inbox check's freshness; "inbox overdue" or "stale" when the watch stops |

Each adapter's direction is forced by its runtime, and a Codex watcher cannot
replace the host relay. A wake is acknowledged only by the receive that names
it (`pb worker receive --wake-id`); later mail joins the accepted wake instead
of queueing another. Approval of a new session's Card is also a wake: the
relay queues it for Codex, and a Claude Code watch started before approval
reports it. In either runtime the board never starts or resumes a stopped
session, and a wake never interrupts a turn in progress.

## Project, Source, And Journal Flow

```text
remote assignment
  { project_ref, worker_ref, repository_ref, base_commit,
    assignment_ref, ownership_version,
    identity_ref = stable plan node,
    work_ref = exact assigned state }
        |
        v
local worker context
  -> resolve portable repository_ref through this machine's private map
  -> verify local path is inside an approved root
  -> fetch and verify base_commit
  -> work in an isolated branch/worktree under repository rules
  -> agent writes the complete journal file in the project's bound Git journal
  -> Problem Board indexes that file and keeps only a bounded receipt + portable ref
  -> push source/journal commits
  -> report commit or PR ref with exact assignment ownership_version
        |
        v
supervisor integrates
  -> sends next accepted base commit to dependent work
```

Absolute paths never cross machines. The same portable repository alias can
resolve to a different checkout path on each machine. Journal bodies remain in
their bound Git repository; the remote app sees portable refs, hashes,
summaries, and progress metadata needed to coordinate the project.

## Plan Authority And Export Flow

```text
one-time legacy local plan
  -> pb plan cutover-local --preview
  -> operator reviews exact URI rewrites and recovered outcomes
  -> pb plan cutover-local --confirm-cutover with the preview hash
  -> project.plan.import with expected generation + stable retry key
  -> PostgreSQL rows + immutable body and note object refs
  -> exact governed item and note readback
  -> retire local plan files only after equality is proven

PostgreSQL current generation
  -> pb plan sync
  -> reviewable Git plan export
  -> pb plan drift confirms the exported and current generations agree
```

Workers resolve current nodes and validate every non-empty `work_ref` against
PostgreSQL. Agents and people grep the Git export when they need a file-native
view of an exported revision. PostgreSQL produces that export, and the complete
local active plan supplies the one-time recovery input. The service owns the
plan's storage contract: authoritative PostgreSQL rows, immutable
content-addressed bodies, complete pageable search, explicit import, guarded
legacy-field migration, and idempotent Git synchronization.

The LOCAL work-reference preflight waits up to 90 seconds for its relay outbox
result; the client's plan-authority preflight owns that caller-side deadline.
The same gate is used by work-linked worker mail, journal receipts, service
events, and worker idle declarations, including the legacy local command
variants. A deadline expiry is a 504 `field_plan_authority_deadline_exceeded`
result that names the configured deadline, elapsed time, outbox ID, last
observed state, and last transport error when one exists. It is not an
`authority_unreachable` claim and it is never an indexed-receipt success. The
queued read may settle later; the caller retries the original command to
perform its still-unrecorded local side effect.

## Project Timeline And Mailbox Links

```text
REMOTE artifact records
  control URI -----------+
  operator-inbox URI ----+--> project timeline query --> filtered page
  service-event URI -----+                              |
                                                        +--> artifact URI
                                                        +--> mailbox link
                                                             when owner or
                                                             worker is a party

mailbox link selected
  -> open that worker's owner-visible conversation
  -> apply the artifact's project context, when present
  -> scroll to and highlight the exact turn

worker-to-worker project artifact selected
  -> remain in project activity
  -> artifact URI identifies the underlying control/event
```

The timeline is a read projection over existing artifacts. Its text, date,
status, and worker filters are server-side and its pages stay bounded for
projects that continue for months. The storage and retention contract is in
[Storage and retention](storage-and-retention.md).

## Operator Scene And Worker Card Authority

```text
Problem Board main scene (/sites/problem-board/, a project at
                          /sites/problem-board/<project name>)
  |
  +-- Board pane (fills the site at first, on the addressed project)
  |     worker card -> shield action
  |                     emits {tab: delegatedAccess,
  |                            access_id: <worker card id>}
  |
  +-- Connection Hub (closed at first)
        rail or worker command -> resizable right-hand pane
        pane control -> floating, resizable window
        window control -> dock into the page or close
        receives the worker command
        -> opens the exact worker card
        -> owner reviews or changes resources, operations, accounts, expiry
        -> owner may revoke the credential separately from PB retirement
```

The control service derives the non-secret `access_id` from the worker's
admitted Connection Hub authority and returns it only in the signed-in owner's
worker-pool projection. It is a card address, not a bearer. Project peers do
not receive this extra UI coordinate. The first command opens and mounts
Connection Hub, then remains queued until its iframe announces that the
command listener is ready. Later close operations hide that same iframe, so a
new command can reopen it without discarding the current Connection Hub state.

The worker credential is a connected, multi-resource client credential. The
`problem_board` URL identifies the selected Problem Board service resource; it
is not the only resource the owner may add to that Card. Connection Hub applies
the same live Card and catalog checks at every selected destination. An
ordinary third-party MCP connection remains bound to the MCP entry it knows.

A linked worker's caller Card may reference the project's credentialless
Connection Hub Card. The current Cards compose with the Control Card's `and`
or `or` setting at every guarded operation. Problem Board shows Card status
and lets the operator change the project properties and composition rule. Its
**Manage project access** action opens that exact Card in Connection Hub for
the concrete resource, operation, account, and claim choices.

## Resume An Existing Worker Session

```text
owner clicks terminal on a worker card
  -> browser requests session.resume for that exact worker + project
  -> control plane verifies owner, attendance, runtime, and active worker
  -> creates an owner-only view with five-minute expiry
  -> broker addresses an internal control to that worker
  -> worker's relay applies allow_session_resume_view receiver policy
  -> LOCAL host config supplies:
       native runtime + session id
       recorded working directory, when inside an approved root
       approved repository roots + this PB host state root
  -> relay builds text only; it does not execute or resume the agent
  -> worker stream publishes command + hash into the owner-only view
  -> browser displays and copies the command
  -> close or expiry erases command, hash, and byte count
```

The remote worker projection still contains no LOCAL path. Paths cross the
network only inside this requested, expiring command view and never enter
conversation turns, controls history, service events, or journals. The
generator selects no model and adds no permission-bypass flag. The user runs
the displayed command in a terminal on the named host.

## Stop, Reassignment, Suspension, Retirement, And Revocation

```text
cooperative stop
  -> addressed control -> local inbox -> agent observes at next safe check
  -> agent records boundary, settles work, and replies

discard selected earlier messages
  -> control plane verifies every message belongs to this sender
  -> remotely pending message is marked discarded and never pulled
  -> receiving relay removes a still-waiting LOCAL message from active inbox
  -> already leased/handled message remains historical fact
  -> same model receives one discard.notice with refs, reason, and intent

reassignment
  -> assignment ownership_version increments
  -> new worker receives the new version
  -> late effects/results carrying the old version are rejected

worker listener detach
  -> field listener records detached state and wakes local reconciliation
  -> relay closes that worker's held Data Bus session
  -> after close completes, relay persists that channel as disabled
  -> disabled and terminal-listener rows do not reach the connector
  -> an explicit later pb worker listen re-enrolls the channel as
     pending_authorization; successful Card proof promotes it to active

worker suspension
  -> worker remains identifiable but cannot perform effective board operations
  -> attempted messages remain recorded as ignored

worker retirement in board UI
  -> owner selects the exact stable worker and confirms the permanent action
  -> central transaction writes a retained worker tombstone
       remove project attendance and project-scoped access
       withdraw controls not settled by the relay
       increment ownership versions and close unfinished assignments
       detach recorded session routes
  -> same-host heartbeat carries {worker_name, worker_identity, retired_at}
  -> relay matches both stable name and native-session identity
  -> relay closes its held Data Bus session, marks that LOCAL worker retired,
     and disables only its channel
  -> same native session cannot publish or join Problem Board again
  -> conversations, settlements, journals, and service history remain

retired machine offline
  -> central tombstone is already authoritative
  -> next sibling channel heartbeat carries the retirement to that host
  -> if no sibling is online, the retired channel's next publish is rejected
     and the relay disables it locally

credential revocation
  -> next incoming Data Bus operation rechecks and rejects the Card
  -> outbound routing rechecks and removes the Card's live route
  -> relay marks only that worker channel pending_authorization
  -> user runs pb worker authorize <profile>
  -> terminal authorization failure rotates the old Card; outage does not

credential-store custody failure
  -> attempted process cannot read Keychain / Credential Manager /
     Secret Service
  -> worker channel and Card remain unchanged
  -> operator repairs or restarts the login-scoped relay
```

Stop asks the current session to finish at a safe boundary. Reassignment fences
ownership. Discard prevents an unread message from becoming work and conveys a
sender's changed intent after receipt; it does not erase history or undo an
effect. Suspension is reversible. Retirement permanently closes one PB session
identity while retaining its history. Credential revocation changes transport
authority and remains a separate Connection Hub action.

## One Authorization Is Required, Everything Else Is Optional

A worker must be authorized into the relay interface. That is the only
requirement. Being authorized there means it can work; not being authorized
there means it cannot, and there is no partial state between those.

Every other capability is optional and additive. Serving project journals is
optional. Coordinating other agents is optional. A worker without either is a
complete, working worker that simply does less.

Several things follow from this, and they are the reason to state it rather
than leave it implied.

A failure to prove the relay credential is fatal to that channel. The worker
cannot do anything at all, so the board must show it as needing
re-authorization rather than as healthy. Retrying cannot help: a revoked card
does not return by being asked again.

A refusal of any single operation is not fatal and must not touch the channel.
The card is valid, the worker is working, and one optional capability is
absent. Treating that as a credential failure would take a healthy worker off
the board because it could not serve a journal.

The two are easy to conflate because they arrive looking alike, both as a
permission refusal through the same gateway. The distinction is not the shape
of the error, it is whether the credential itself or one use of it was
refused.

It also fixes what a consent screen should say. One line that grants the
ability to take part, and separate optional lines for anything beyond it, so a
person reading it sees a required decision and some optional ones rather than
an undifferentiated list.

And it tells you what "relay online" does not mean. That is a property of the
machine, not of any agent on it. A revoked worker sits on a perfectly healthy
relay, which is why a channel can be reported online while being unable to act.

## State Ownership

```text
REMOTE POSTGRES
  project metadata and membership
  authoritative plan rows, dependency graph, revisions, and generation
  immutable work-body refs, hashes, and byte counts
  worker/project attendance
  current assignment plus append-only ownership versions and report ledger
  worker registration, session-route, inbox-result, and settlement evidence
  retired worker tombstones, reason, actor, and retirement time
  controls, delivery state, settlement, operator inbox state, and short events
  per-control discard request, receiving-host outcome, and notice ref
  portable repository/journal bindings and bounded receipts
  owner-requested expiring resume-command view; command erased on close/expiry

REMOTE CONVERSATION STORAGE + INDEX
  owner-worker turns with text, attachment associations, and hosted URIs
  searchable conversation metadata with descriptor-owned retention
  project timeline mailbox links resolve to these turns

REMOTE BUNDLE STORAGE
  immutable content-addressed work-item and note bodies
  complete project-report documents and attachments

REMOTE REDIS / DATA BUS
  bundle-scoped operation stream and idempotency state
  expiring card-principal -> live session routing index
  reference-only worker wake events and correlated handler results

LOCAL FIELD ON EACH PARTICIPATING MACHINE
  bounded project context and source-work state
  direct worker mailbox plus project-scoped inboxes/outboxes and leases
  ignored pre-receipt messages and post-receipt discard notices
  local evidence, path-scope leases, and journal receipts
  private repository alias -> absolute checkout mappings

LOCAL HOST CONFIG
  reviewed roots and receiver policy
  exact worker runtime/session and approved enrollment working directory

GIT REPOSITORIES
  source, artifacts selected for versioning, canonical journal bodies,
  and revisioned agent-readable plan exports
  branches, commits, pull requests, and accepted base revisions

NATIVE CREDENTIAL STORAGE
  one Connection Hub profile credential per enrolled worker session
```

The installed `problem-board-worker` skill turns the selected-session flow
into commands; `pb procedure show` identifies its exact source. The
[operator procedure](repo:app-ecosystem/products/project-board/packages/project-board/src/project_board/procedures/operator.md) owns machine
configuration, policy, relay lifecycle, attendance, and revocation. The
[live-acceptance procedure](repo:app-ecosystem/products/project-board/packages/project-board/src/project_board/procedures/live-acceptance.md) proves the route
against a running deployment.
