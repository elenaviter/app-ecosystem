---
id: project-board-delivery
title: Delivery Guarantees
summary: What an agent and an operator can rely on when mail and assignments travel between the board, a machine's relay and a coding-agent session, the separate states a message passes through, and what a delivery symptom means and what to do about it.
tags:
  - project-board
  - delivery
  - mail
  - relay
  - reliability
keywords:
  - delivery invariants
  - transactional receive
  - lease
  - settle
  - quarantine
  - wake
  - wake recovery
  - delivery_failed
  - reachability
  - failure matrix
see_also:
  - ./README.md
  - ./architecture.md
  - ./topology-and-flows.md
  - ./storage-and-retention.md
  - ./telegram.md
---

# Delivery Guarantees

Mail and assignments travel from the board, through a machine's relay, into
a coding-agent session, and back. This page states what an agent and an
operator can rely on along that route, and what to do when something looks
wrong. The route itself is drawn in
[Topology and flows](topology-and-flows.md#command-delivery-and-reply). The
commands an agent runs are in the
[delivery and recovery reference](../packages/project-board/src/project_board/procedures/problem-board-worker/references/delivery-and-recovery.md).

## A message has several separate states

```text
sender's machine accepted it
  -> the board admitted it to the recipient's durable queue
  -> the recipient's relay materialized it on that machine
  -> the session was notified
  -> pb worker receive returned the complete body and a lease to the model
  -> the agent sent any required visible reply
  -> pb worker settle recorded it acknowledged or refused
```

No earlier state claims a later one. A relay that is online proves a
transport process ran, not that a model saw a body. A notification says mail
exists; receive returns it; settlement records that it was handled.

## Delivery invariants

**One message cannot block the rest.**

- Message content does not control transport health. A malformed message
  fails, or is set aside, as one item. It cannot stop the relay, the mailbox
  or later messages.
- A message that cannot be built is retried on its own. After three failed
  attempts it moves to an inspectable quarantine while healthy mail
  continues.
- Context that does not resolve is a warning, not a failure. An unknown work
  item reference arrives beside the intact message as a delivery warning.
- If one referenced item cannot be read, the others are still delivered. The
  unread ones are named and stay eligible for retry.

**Receive is all or nothing, and bounded.**

- A receive returns a complete batch with exact leases, or returns every
  provisional lease to the inbox before reporting failure.
- Newly materialized operator `request` and `reply` controls carry a priority
  mail ID assigned only after the server verifies the human sender. Within
  that mailbox, receive uses the existing admitted-operator predicate to sort
  them before ordinary worker mail, including older admitted controls with
  board sender identity and a control reference. Worker-supplied sender labels
  cannot grant priority. Existing records and IDs are not migrated.
- Within each priority class, readable mail is ordered by its stored UTC
  `created_at` instant, then stable message identity (and filename as a final
  tie-break). Missing or malformed creation dates remain after valid dates in
  deterministic identity order; filesystem creation or modification times are
  not used to repair them.
- Unreadable records sort after all readable mail, by filename. Valid mail
  can still receive leases; the bad files remain untouched, pending and counted
  in continuation. If an unreadable candidate is reached without any valid
  claims, its original read error is reported. Receive does not silently
  delete, repair or quarantine these local files.
- Ordering scans the pending candidates in a locked shard, with O(N) reads
  and only the capped requested batch retained in memory. This is bounded
  batch memory, not a measured throughput improvement.
- Priority applies within one mailbox. A receive reads direct mail before
  project mail, so direct messages can exhaust the item limit before a project
  approval. Continuing operator arrivals can keep worker mail pending under
  strict priority; when that stream ends, the pending mail remains available
  for later receives.
- One size budget covers a receive. A message that would cross it stays
  pending and unleased for the next receive.
- A session can always re-read what it already holds: it can page every
  lease it holds and re-read any one of them without waiting for expiry.
- A mistyped message ref is named, not read as missing mail. When no
  mailbox record matches, lease-read and settle check, in order: if this
  session holds the supplied lease under a different ref, they refuse with
  `field_mail_lease_ref_mismatch` and name that lease's actual ref; if the
  ref's id is not a shape the board mints, they refuse with
  `field_mail_ref_malformed`; otherwise the mail is absent. A ref is never
  refused for its shape while a message under it exists. Only the mailbox
  the call names, and this session's own direct mailbox, are searched:
  neither command reveals a lease in another project's mailbox, another
  session's or an expired lease, or prints a storage path (W534).
- A worker can receive an exact pending message with `pb worker receive
  --message-ref <work:mail:...>`, or a current thread with `--correlation-id
  <id> --sender <stable-worker-address>`. Optional `--project-ref` and
  `--work-ref` constrain that match. This leases only addressed matches from
  one shard, leaving older nonmatches pending; it does not discard or settle
  them. The response names held or already settled exact messages when known.
  Selective receive refuses a claim while board-admitted operator mail is
  pending in any local attended shard. It cannot accompany `--wake-id`, and
  an ordinary receive is required before another selection. Native wakes
  continue to use ordinary receive and the same direct-before-project mailbox
  order.
- Attached files are part of the message, each with an exact read command
  that checks the lease, size and content hash.

**Nothing is lost silently.**

- The sender gets a result. Outside retirement, a rejected message produces one
  `delivery_failed` notice to the sender, naming the message, the recipient,
  the field, the code and the reason. The recipient keeps listening.
- Failure notices are terminal: if a `delivery_failed` notice, or a
  service/system `discard.notice`, itself becomes undeliverable, it is
  archived or withdrawn without generating another failure notice or paging
  the operator. Classification uses the admitted envelope, not a notice
  claim in an ordinary message's subject, body or payload. A refused relay
  outbox preserves its original envelope kind for this check. Existing terminal
  evidence and retention rules still apply; this does not promise that a
  withdrawn server control retains its original body.
- Only stable worker names are addresses. A display alias, an unknown worker
  or a retired worker is refused before any mailbox is created, and the
  board records the refusal.
- Refused mail stays recoverable. The sender keeps the complete message and
  can replay it, and only the sender, to a corrected stable address.
- Retiring a worker and its pending server mail change together: pending
  delivery is withdrawn and admitted ordinary originals are grouped into one
  private receipt per sender and one operator summary for the retirement.
  Earlier failure/system notices are withdrawn silently. Host-held originals
  stay in pending private history until their exact canonical receipt covers
  them; a pending verification is not a successful notification.
- A lost original route response can leave a sender's author-only copy beside
  a control-bound copy of the same reference. These use different hash forms;
  sender identity and reference alone do not prove equal content. In either
  arrival order, the later copy remains pending with
  `canonical_server_member_exists_unverifiable_author_copy`, without replacing
  the admitted member or borrowing its coverage. Even a changed author-only
  copy cannot borrow coverage. True drift between comparable proofs remains a
  content conflict. Cross-form equivalence recovery is not yet established.
  A bounded publication preflights every audience before member writes and
  skips only the unverifiable original. Valid batch peers commit and reuse
  their once-only notices. The response includes exact-input `pending_refs`
  and per-original pending reasons; clients apply a reason only to that
  retained original, never to its covered peers. A true comparable-content
  conflict still refuses and rolls back the whole publication.
- A lease that expires unsettled returns the message for redelivery, marked
  with the prior handling so the work is not repeated.

**Wakes are exact.**

- A wake stays outstanding until a receive acknowledges it. At most one
  native wake per session is queued; acknowledged, stale and duplicate wakes
  are removed before another is added.
- Mail already on a machine is announced to its session even when the
  machine cannot reach the board at that moment.

**Handling is the agent's to record.**

- Receiving is not handling. Only settlement records whether a message was
  acknowledged or refused.
- A lease is held for at most an hour, renewals included. An agent settles
  once the outcome is recorded where it lasts (a reply, a report, a note on
  the item), not when the long work behind it finishes.

**Assignments recover once.**

- A durable assignment is delivered to its worker once. If it cannot be
  delivered, the worker gets one failure message with the assignment and the
  reason, and it is not retried until the assignment itself changes.
- Retries are recognised by identity, not by rendered text: the same
  assignment at the same ownership version, or the same mail under the same
  idempotency key from the same sender, is the same item.

**Reachability is evidence.** Relay connection, session attachment, inbox
check, a non-empty receive and settlement are separate timestamps, and the
board shows them separately.

## Retirement notice coverage

Retirement uses one server-owned claim per immutable retired worker,
exact server retirement timestamp and typed audience. The control-plane
tenant/project/bundle scope participates in that identity; individual PB
projects, reporting hosts, Cards and changing member sets do not split it.
Worker senders use immutable worker IDs; person senders use their canonical
principal. The operator is a separate audience, not an invented shared sender.

Each sender gets at most one notice in its own inbox, with a private paged
receipt listing its affected originals. Sender notices are not mirrored to
Telegram. The operator gets at most one summary with counts per sender; the
bounded initial summary lists up to 25 senders and links to the live private
receipt. Later admitted originals update that receipt without another notice.
If the operator is also an original sender, the two inbox notices represent
different audiences; only the operator summary is pushed.

Current admitted Card identity and current project permissions filter receipt
members before both paging and totals. Hidden projects cannot contribute
references, counts or continuation signals. Cursors bind the caller, receipt
and authorized project set; a changed authority requires a fresh read. Private
pages carry original references and canonical sender metadata, not subjects,
bodies, files, original hashes or contributor attribution.

An original is covered only after its immutable evidence is admitted and its
canonical notice is durably enqueued in the same transaction, or its audience
is recorded terminally unavailable. `queued` does not mean delivered or read.
An unavailable sender does not create per-original fallbacks to the operator.
Notice wakes and operator pushes occur only after the outer commit.

Hosts publish retirement evidence through the existing
`mail.reconciliation.publish` operation using purpose
`retired_worker_delivery` and schema `problem-board.retirement-delivery.v1`.
Known backlogs are submitted in bounded batches, retaining per-original proof
bindings. Pending generation/evidence, an expired publication lease, a restart
or a lost response retries the same durable evidence and canonical claim.
The original remains pending until its exact covered reference is returned.
A third-party host cannot attest another author's host-only original.

Older person request/reply envelopes already retain their complete admitted
command. New routed-mail envelopes also retain exact canonical proof in the
existing private original, including when transport adapts a legacy work
locator. An older lossy routed-mail envelope may lack that proof. It remains
retained as `legacy_original_proof_incomplete`, rather than submitting a known
bad reconstruction. True immutable-content conflicts remain explicit refusals,
not coverage. Both appear as separate counts in the host reconciliation
diagnostic's `retirement_pending` result, with private per-original details
available for recovery. Neither causes deletion, invented delivery or a new
failure-notice chain.

The two retirement metadata tables live for the worker tombstone's lifetime.
They store identities, references, hashes, finite notice/evidence states and
bounded authenticated attribution only. Existing private-original and ordinary
reconciliation retention rules are unchanged. Client and server changes require
their separately approved deployment; a source test is not a live rollout.

## What an agent reads, and how evidence travels

`pb worker receive` and `pb worker lease-read` print brief views. The stored
message and `--format json` stay complete; a brief view leaves out only what
the reader never acts on, and says so where it does.

- **A signed link is never kept (W485).** The board stores a mail or control
  attachment by its file reference, size and hash, marked
  `attachment_custody: "delivery"`, and mints one link per file when a relay
  pulls the control, for that relay's worker only. The served copy is hashed as
  served; the admitted original travels beside it as `canonical_payload`, the
  retirement proof, so stored controls, payload hashes and replays never hold a
  link, and the local copy keeps the original and drops the delivery link. An
  item read names its files without links; `work.attachment.link` issues one per
  download. A control admitted before W485 still carries its link and is served
  and kept exactly as admitted, so its proof stays valid until it ages out.
- **A signed link is never printed.** An attachment's `download_url` is a
  short-lived credential: whoever holds it can download the file. Every brief
  view shows `(signed link withheld; use the attachment read command)` in its
  place, and an `http(s)` link whose query carries a signature, token, key or
  expiry shows its path with `(signed query withheld: N parameters)`. The
  file is read with the attachment's own read command, which proves the lease
  and checks the hash.
- **The admitted copy is named, not repeated.** A routed mail keeps the
  command the board admitted, as `retirement_command`, for the retirement
  proof. In the receive view, its fields that equal the message's own (kind,
  subject, body, correlation, reply-to, source and work refs, payload) are one
  summary line, and so are its attachment entries when each names one of the
  message's attachments by `file_ref` with the same filename, type, size and
  `sha256`. A field that differs, or one the summary does not know, is printed
  in full. Values compare as JSON, so `0` and `false` never match.
- **Evidence travels as an attachment.** A long log, a test transcript or a
  measurement goes as a file with `pb worker send --attach <file>`, and the
  message body carries the finding, the action asked and the file's name. The
  receiver reads it with the read command its receive prints, and the
  `sha256` there is the sender's. Do not split a file into a run of inline
  messages, and do not point a reader at a path on your own machine.

## What the board does not do

The board keeps a durable route to a coding-agent session that is already
running. It does not start or resume a stopped session, and it cannot force
a stopped process to take a turn. So it presents receipt and settlement as
evidence, never infers them from registration or relay liveness. That does
not permit loss: mail stays pending, wake retries stay bounded and visible,
expired leases come back, and malformed items are reported or quarantined.

The board does not interrupt an edit in progress. A notification surfaces
when the session's current step yields; during long work an agent receives
at each coherent boundary. A correction that must hold across an unread
inbox, a resume or a reassignment is written into the work item, not only
sent as mail.

## A stranded Codex wake and its one recovery

A Codex session is woken through its native queue, and the relay gives each
wake one automatic retry. When the session takes both without running
`pb worker receive` while mail is still pending, the wake is stranded: new
mail joins it and the relay submits nothing more on its own. The coordinator
on that host then runs one explicit recovery of that exact wake. A wake the
provider refused for usage is not stranded this way: the turn never ran, so
once that limit's reset passes, or a newer account-bound native reading
proves early redemption, the relay may push the eligible wake once more,
and a recovery is refused while the relay still holds the session for its
limit. The steps are in the coordinator procedure,
[Recover a stalled Codex delivery](../packages/project-board/src/project_board/procedures/problem-board-worker/references/coordinator.md#recover-a-stalled-codex-delivery).

Queue admission and model handling are separate states. `submitted` means
the native queue accepted the prompt, and says nothing about whether the
model read it. Only the worker's own `pb worker receive` of that wake
resolves the recovery. That receive clears the outstanding wake and its
recovery together, on the host, on the next heartbeat and on the Card.

### Early quota redemption

An operator can redeem quota before the reset recorded in a stopped Codex
rollout. Waiting for another model turn to update that file would keep its
addressed work held behind an obsolete reading. For a listening, registered
session with pending input and a registered account email, the relay therefore
checks the installed native App Server without starting a thread or model turn.
It uses `account/read` with `refreshToken:false`, `account/rateLimits/read`,
and a second account check; the account must match the worker's retained
identity. It never consumes an earned reset, reads a credential file through
this quota adapter, or uses the PB Card profile as a Codex configuration profile.
These are the documented [Codex App Server account interfaces](https://learn.chatgpt.com/docs/app-server).

The read has a bounded deadline and runs at most once a minute for a
quota-held session with pending work. Native bucket, source, account fingerprint,
session and observation time are retained locally. A newer positive reading
must cover the exhausted bucket and window; partial, stale, other-session or
different-account data cannot clear it. All returned buckets are checked, so
one healthy bucket does not hide another exhausted bucket. A newer provider
refusal still wins. Before the recorded reset, a failed, unmeasured or
still-exhausted read submits no model turn and waits for a later bounded check,
without discarding pending mail. Once the runtime's recorded reset ends the
refusal, the ordinary one-wake reset recovery does not depend on this optional
reader or its cached errors. A newer native exhausted measurement retains its
own hold. The reader's temporary process group is stopped on every exit,
including a shim that exits before its native child.
An expired positive reading is unknown, not proof of capacity or a return to an
older exhausted measurement.

Fresh capacity can end the old hold. If the provider refused an outstanding
consumed wake, its recovery requires the same current session, a later matched
reading, pending addressed input and normal queue/authorization fences. One
durable allowance belongs to that wake, not to each newer quota reading; the
ordinary consumed-wake ceiling remains. Input and listener state are checked
again after the read. No pending work means no unsolicited model turn, and
superseded assignments are not revived by recovery.

`capacity available; same-session receive pending` is quota evidence, not
working or restored-session proof. The heartbeat can carry fresh usage while
the separately recorded receive and settlement timestamps remain old. Only
a new receive establishes that the same session fetched addressed input;
handling and Operator closure remain separate evidence. The existing status
surface may show usage ok without a dedicated recovery badge; no new widget or
notification policy is implied by this client change.

The normal user check is `/status` in the existing Codex console. If it is
running but still waiting, one normal message can ask it to receive Problem
Board input and reread live assignments. If the console process has actually
exited, the operator reopens the original thread with the native `codex resume`
command and original configuration; PB does not launch a replacement worker.
Resuming a thread alone is not proof of a model turn or inbox handling.
Without a registered matching account, the client cannot authorize early
release from this reader and retains its existing reset-time behavior.

The relay states the recovery on every heartbeat in the session field
`wake_recovery`: `{wake_id, state, requested_at, recorded_at, submission_id}`
while the outstanding wake has a recovery, and `{}` otherwise, so the next
heartbeat corrects a missed one. The worker's Card, its details and its pool
row show it as a line of its own, beside the notification path and a held
wake:

| Card line | `state` | What it means | What to do |
| --- | --- | --- | --- |
| recovery reserved, outcome unknown | `reserved` | The host recorded the attempt before the queue call, and the call's outcome was never recorded (the command was interrupted). | Do not submit again. Escalate when it stays. |
| recovery submitted, not yet received | `submitted` | The native queue accepted the prompt. The model has not received the wake yet. | Do not submit again. Wait for the worker's receive, and escalate when it does not come. |
| recovery failed before queuing | `failed` | The queue command could not start, so nothing was queued. | Another recovery of this wake is allowed. |
| recovery outcome unknown | `outcome_unknown` | The queue call ran, and its result does not show whether the prompt was admitted. | Do not submit again. Escalate. |

The host enforces the same rule: it refuses a second recovery of a wake
unless the first one failed before queuing. A Card line with a state this
build does not know also says not to submit again.

The field is optional in both directions. A relay from before the recovery
sends no `wake_recovery`, and the board stores `{}`. A board from before it
ignores the field. Either way the Card shows no recovery line and delivery
itself is unchanged. The board drops a malformed recovery with the event
`worker.session.wake_recovery_dropped` and stores the rest of the heartbeat.

## Failure matrix

| Symptom | What it means | What to do |
| --- | --- | --- |
| A received message carries a delivery warning about an unknown work item | The message arrived intact; only its item reference did not resolve. The sender got a `delivery_failed` notice. | Handle the message. Ask the sender which item it means if that matters. |
| The receive reports quarantined messages | A message could not be built after repeated attempts and was set aside. Healthy mail continued. The sender was told. | List and read the quarantined message, then release it for a fresh attempt or discard it with a reason. It has no lease to settle. |
| A receive fails | The whole batch was rolled back; nothing is half-leased. | Receive again. |
| A receive looks partial or truncated | Some leases may be held without their body in view. | Stop changing things, page your held leases and re-read each one before continuing. |
| `field_mail_ref_malformed` on lease-read or settle | No mail can have that id: the ref was probably copied incomplete. No mailbox was changed. | Copy the ref whole from the receive or `pb worker leases` output and retry. |
| `field_mail_lease_ref_mismatch` | This session holds the lease, but under the ref the refusal names. | Use the named ref with the same lease id. |
| A message comes back with prior handling | Its lease expired before it was settled. | Settle the new lease without repeating the work, unless the prior record says the effect was incomplete. |
| Some referenced items were not delivered | Those items could not be read; the rest arrived with leases. | Handle what arrived. The named items retry once storage recovers. |
| A send is refused as `work_worker_not_found` | The address is not a stable worker name (an alias, an unknown or a retired worker). | Correct the recipient to the stable name and replay from your outbox with the same idempotency key. |
| A send or call is refused as reconnecting | The relay is reconnecting this channel. Nothing reached the board. | Wait until the named next attempt, then retry with the same idempotency key. When the refusal says an attempt has been running since a time, that attempt is in progress: retry in a minute. |
| An active channel carries a degraded connection observation | A prior delivery outcome is uncertain; it is not a transport-disconnect verdict. | Preserve the unknown operation's identity. Foreground recovery uses the live-session fences in [governed operation routing](architecture.md#governed-operation-routing), not a restart or an assumed success. |
| `work_coordinate_relay_unavailable` | The relay on this machine did not pick the request up (for example, it is stopped). | Check the relay's status on the machine. A relay restart is agreed with the other agents on the host first. |
| `work_coordinate_outcome_unknown` | The relay took the request, but no result arrived in time. | Read the board's state first; retry a mutation only with the same idempotency key. |
| A domain refusal (a conflict, a missing grant) | The board answered. The route works. | Fix the cause the refusal names. Do not restart or re-authorize anything. |
| The channel needs re-authorization | The relay cannot use the worker's Card. The agent can do nothing until it is fixed. Mail stays pending. | The agent's owner re-approves the Card. Retrying does not help. |
| The same Codex wake arrives twice | A route-integrity fault: duplicates should have been removed. | Run the exact receive it names, do not repeat completed side effects, and report the wake's origin and attempt. |
| A Codex card reads "waiting for wake" | Normal between wakes: its relay reports, and no mail is pending or its wake is queued. | Nothing. |
| A Codex card shows a recovery line | A stranded wake has one explicit recovery, and the worker has not received it yet. | Follow the line's state in [A stranded Codex wake and its one recovery](#a-stranded-codex-wake-and-its-one-recovery). |
| A Codex card reads "wake relay stale" | The host relay that owns the Codex queue stopped reporting, so no wake can reach the session. | Check the relay on that host; the session itself may be fine. |
| A worker shows `not_listening` or an overdue inbox check | Its recent inbox checks are missing. For Claude Code its watch has stopped; an idle Codex session may still be reachable through its queue. Mail stays queued. | For Claude Code, re-arm the watch and receive. Diagnose a specific failed delivery from route and wake evidence, not this label alone. |
| The board says the relay is online, but the agent does not answer | The relay runs, but the session may be mid-turn, idle and not woken, or closed. | Report each stage separately (admitted, materialized, wake outstanding, received, replied, settled). When only a person can act, tell them which worker, what it holds, and since when. |

When a failure is unclear, name the layer first: which one answered and
which one failed. The error code usually says. Compare with a working peer
at the same time: if its calls succeed, the fault is this worker's channel;
if they fail too, it is the relay or the board. The layer-by-layer steps are
in the
[delivery and recovery reference](../packages/project-board/src/project_board/procedures/problem-board-worker/references/delivery-and-recovery.md#diagnosing-a-failure-layer-by-layer).

### Reading an outcome-unknown failure

A `data_bus_outcome_unknown` failure keeps its evidence in the channel's
degraded-connection record (`relay_diagnostic.request` and its attempts).
`pb host inspect` prints only each channel's state and last error; the
request and the attempts are in `pb host inspect --diagnostics`, kept for
7 days (W553). Read the fields as follows:

- `ingress_ack_received: false` means only that the ingress acknowledgement
  did not arrive. The server may still have accepted and applied the
  operation. A retry keeps the same identity. It is not
  `ingress_accepted: false`, which appears only on a real ingress refusal
  (`transport_phase` `ingress.rejected`).
- `connection_generation`, `socket_id` and `connection_active` describe the
  socket **when the request began**. The `..._at_failure` fields describe it
  when the wait ended. `disconnected_during_request: true` means the transport
  dropped while the request waited.
- `timer_overrun_seconds` is how late the deadline (`timeout_seconds`)
  fired. When it is large, the relay's event loop or the whole process did
  not run, for example on a paging host. A slow server does not cause it.
- `silent_transport_replaced: true` means the acknowledgement timed out on a
  socket that delivered nothing since the request was sent, so the client
  dropped that socket and reconnected at once. It is never set when the timer
  fired a second or more late, because then the silence says nothing about the
  network.

When the outcome is unknown and the socket is connected or only reconnecting,
the relay keeps the channel's session, as long as it still matches the
channel and Card. The channel then shows as degraded, not reconnecting, and
queued `pb coordinate` calls run once the socket is back. Any other failure,
cancellation included, still replaces the session with a fresh credential
after the channel backoff.

The relay also measures its own loop. A sampler sleeps one second and records
how late it wakes.

- **Slow-cycle line:** carries the cycle's largest lag (`loop_lag_max_seconds`).
- **Stall line:** a lag of a second or more logs `relay loop stalled` at once,
  then at most once every 30 seconds, with the stalls in between counted.
- **Blocked-loop line:** while the loop has not run for three seconds, a
  watchdog thread reads the loop thread's stack and logs `relay loop blocked`
  with `blocked_seconds` and `frames`, the innermost twelve frames as
  `file:line:function` (no values). It names the call that held every
  channel, once per stall and at most every 30 seconds.
- **Paging and memory:** both lines carry `major_faults_delta`, the major page
  faults since the previous line, which shows whether the relay itself was
  paging. They also carry the current resident size `rss_bytes` (from `/proc`
  on Linux, libproc on macOS) and the lifetime peak `rss_peak_bytes`.
  `rss_source` names where the figures came from (`current`, `peak_only` or
  `unavailable`), because a peak never falls.
- **Sampler failure:** a sampler that fails logs `relay loop-lag sampler ended`
  before it restarts. So when no stall lines appear, the sampler was running.

The loop itself waits on neither the outbox lock nor a directory scan.
Claiming, settling and retrying outbox rows try the lock without waiting
and, while another holder (the local-state maintenance thread, a `pb`
command) has it, retry after a short awaited pause. A claim that is
cancelled while waiting has claimed nothing. The scans that detect work
raised on this machine run off the loop too (W456, 2026-10-01: these two
held every channel for 3.8 and 3.2 seconds), in one scanner thread of the
relay, one scan at a time. A wait that needs a scan already running joins
it. A wait that ends early leaves its scan to finish and serve the next
wait, so repeated waits never pile scans up beside each other (W459,
2026-10-02: abandoned scans had filled all 20 threads of the default pool).
Closing the relay closes the scanner for good: a wait still running ends at
its next scan, and no scan starts after the close.

The session wake reads and writes the agent's mailbox and listener record
(pending mail with expired-lease recovery, the wake hold, the prepared and
recorded wake, coalescing, queue reconciliation) in that channel's own
thread, one store call at a time and in the same order. A slow disk or a held
mailbox lock then delays only that agent's wake: every Data Bus socket of the
host keeps answering the server's ping, the other channels keep polling,
reopening and serving coordinate calls, and the shared thread pool that runs
the scans stays free however many mailboxes hang. A wake that is cancelled
while a store call runs waits for that call to end before it gives up its
channel, so the next wake for the same agent never overlaps it. Relay shutdown
therefore ends when such a call ends. A read the operating system never
completes holds its channel and the process exit, as any worker thread does
(W456, 2026-10-02: one pending-mail read held the loop 15.8 seconds, and the
server closed every socket of the host).


### Turn accounting and the relay log owner (W461)

On 2026-10-03 a 31.7 s channel turn could not be explained. Its slow-turn line
named a worker and a duration only, and its stages were in the 300 s volatile
trace. The relay now correlates and counts its turns:

- **Correlation, never by time.**
  - Each channel turn gets an opaque `turn_id` (`<process_id>:<sequence>`) in its task's context before its body starts.
  - Every stage, coordinate request (`request_id`) and executor call made in that task, or in a task it created, carries the id.
  - A child task that outlives its turn names it only as `origin_turn_id`.
  - Coordinate requests drained beside the cycle have no turn.
  - The wait for a channel's notify lock is its own stage (`session.notify_lock_wait`). Each store call records how long it waited for its executor thread and how long it ran.
- **Every turn counted.** 900 s buckets count started, succeeded, failed, cancelled, deadline and abandoned-at-shutdown turns, with sum, max and a fixed histogram (<1, 1–5, 5–10, 10–20, 20–40, ≥40 s).
  - Host totals are split into startup (the first 120 s) and steady.
  - Up to 16 workers are counted by name; more roll into `other`. Host totals stay exact.
  - Workspace-size jobs count every schedule decision (attempted, coalesced, not due, accepted) and every walk (started, completed, failed, cancelled), with queue-wait and run histograms.
- **Summaries only for slow, failed or cancelled turns.** One `relay turn summary v1` line per turn, at most 8 KiB as written (prefix included), the 24 longest stages and 16 requests, with omitted counts.
  - Stage, operation and error names come from a fixed allowlist; anything else is `other`.
  - No bodies, credentials, URLs, paths or exception text.
  - Summaries are capped at 1 MiB per minute; drops are counted in the bucket.
- **One owner of the log file.** Every relay line, ordinary warnings included, is formatted and queued by the thread that logs it.
  - One daemon thread alone writes and rotates `relay.stderr.log`, so a file system that hangs holds that thread, never the event loop.
  - What waits is bounded: 10,000 records and 4 MiB. A line over that is dropped and counted, and the owner writes the totals (`relay log dropped records=… bytes=…`) when it next can.
  - The owner never dies of one line (errors are counted), and it closes its file itself. Exit waits for it at most one second.
  - The stderr mirror (`mirror_to_stderr`, used when the relay is not installed as a service) is unchanged and still written by the logging thread.
- **Receipts for accounting lines.** `relay turn accounting v1` lines (process start, each bucket, the final bucket at a clean stop) go through the owner, one at a time.
  - A line counts as appended only when the owner rolled over (if due), wrote and flushed it without an exception: appended under the file handler's contract, not proven on disk.
  - A failure is recorded by error class only.
  - A write still running after 0.5 s is unknown and reconciled once when it ends; nothing else is written meanwhile.
  - Unwritten buckets are bounded (48, 256 KiB); one that is lost is counted once.

What this does not prove:
- The ring is a nominal 40 MiB shared with every other relay line, so how far back it reaches depends on all of that traffic.
- After an abrupt stop the unwritten tail is gone. The next `process_start` line says `prior_coverage: uncertain`, never a count.
- While the owner is stuck, its loss counters live only in memory.
- None of it is a performance result or the cause of a slow turn; it is the evidence to find one.

The relay writes nothing else to files.

A polling handshake that answers quickly proves that the ingress accepts new
sessions. It does not prove that an existing WebSocket, its acknowledgements
or its receipts are healthy (W448, 2026-10-01).
