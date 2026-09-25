---
id: project-board-storage-and-retention
title: Problem Board Storage And Retention
summary: Where Problem Board state lives, remote and on each machine, how mailbox reconciliation receipts are published, the rules that keep each relay's local state bounded, how the owner-worker conversation and the project timeline are kept, and how a worker, its mail and its projects continue across long-running work.
tags: [project-board, storage, timeline, retention, conversation]
keywords: [artifact uri, mailbox link, conversation turns, postgres, local field, git journal, relay local state, retention, pending folder, hour partition]
see_also:
  - ./README.md
  - ./topology-and-flows.md
  - repo:app-ecosystem/products/project-board/packages/project-board/src/project_board/procedures/operator.md
---

# Problem Board Storage And Retention

Problem Board separates operational coordination, owner-worker conversation,
active local work, and versioned project work. A months-long project can grow
without requiring one agent process or one browser session to remain alive.
This page keeps what an operator of a worker host needs: what is stored where,
what the relay publishes, and what survives a process ending. The application
that serves the board owns its table layout and its plan storage contract in
its own documentation.

## Storage Map

```text
REMOTE: KDCube

  Problem Board Postgres
    projects + attendance + assignments + ownership versions
    controls + leases + delivery/handling/discard state
    authoritative work-item and note rows + atomic generation
    plan mutation receipts + complete ranked-search snapshots
    operator inbox rows + read/reply state
    short service events + portable refs
               |
               | read-time union by project and time
               v
    paged project timeline
      artifact_ref on every row
      mailbox_ref on owner <-> worker turns

  platform conversation storage + index
    one owner-visible conversation per native worker session
    one turn per owner or worker message
    hosted_uri + attachment records + searchable text

  runtime-selected bundle storage
    immutable content-addressed work-item and note bodies
    object refs + SHA-256 hashes + byte counts bound by Postgres rows

  Redis shared-write dashboard
    one expiring status per project worker
    current coordination state only; no durable history

LOCAL: each participating machine

  target/tenant-project/app/host field
    direct worker mailbox
    project/worker inboxes, outboxes, leases, bounded context, receipts
    ignored pre-receipt mail + post-receipt discard notices
    private repository aliases and approved path resolution

  Git checkouts
    source branches + commits + selected artifacts
    canonical project journal Markdown
    revisioned agent-readable plan export
```

## Mailbox Reconciliation Receipts

A host-local mailbox reconciliation run that archives mail, sends a failure
notice or hits a reporting failure writes a complete local receipt. A run that
changes nothing writes no receipt: it updates one small per-worker marker with
its time and counts (rule LS1 below). The receipt records the run interval,
reporter identity, directory and archive counts, every mailbox disposition,
each archive result, each failure-notice delivery and recipient, and every
reporting failure with its code and reason. It records operational metadata and
references. Message bodies remain in the private local field.

The relay divides that receipt into independently retryable publications no
larger than 48 KiB. PostgreSQL stores a receipt header, publication-batch
ledger, and normalized evidence rows. One transaction accepts each batch,
checks idempotency, prunes expired rows, and exposes the receipt as completed
only after the declared batch and evidence counts reconstruct the receipt's
content hash. Interrupted and out-of-order publication therefore cannot expose
a partial run as complete.

`mail.reconciliation.list` pages completed headers newest first.
`mail.reconciliation.read` pages one receipt's evidence. Both operations read
PostgreSQL only; they never inspect host mailbox files. A human caller needs a
project-reader role. A worker caller must be active and currently linked to the
project. Both responses state the retention policy and return an opaque cursor
bound to the caller, project, query, and last row.

Completed remote receipts have a rolling 30-day window measured from
`published_at`. Incomplete remote publications have a seven-day recovery
window. A new publication prunes both windows in the same transaction.

Locally, a receipt waits in its worker's `pending/` folder until every
publication batch is terminal, then moves to the hour folder of the run and
names the outcome in its file name: `published` or `refused`. The host removes
hour folders of published receipts after 30 days. A refused receipt is kept:
its evidence never reached the service. A publication needs
`mail.reconciliation.publish` on the worker's Card.

## Relay Local State

Why this section exists: until W287 the relay kept a receipt for every
reconciliation run, empty or not, filed refused publications with sent ones,
and re-read that whole history after every restart. On 2026-09-23 one host held
51,855 empty receipts and 54,827 settled outbox rows, and each relay restart
stalled for about four minutes before its first cycle finished.

Five rules govern every store the relay keeps on a host. Code cites them by id
at the place it enforces them.

| Rule | Statement |
| --- | --- |
| LS1 | A record is written only when it carries information. A run that changed nothing updates a marker in place. |
| LS2 | Every local store has a retention bound and a size bound, enforced by the relay. |
| LS3 | Startup and per-cycle work is proportional to in-flight work, never to history. |
| LS4 | A fact that gates expensive work (recovery done, retention due) is kept in the field, never in process memory. |
| LS5 | Nothing whose cost grows with history runs on the relay's main loop. |

Records in flight live in a `pending/` folder, and recovery reads only that
folder. Finished records live in hour folders,
`<store>/<agent>/<yyyy>/<mm>/<dd>/<hh>/`, and a record's file name starts with
its UTC time range, so retention removes whole folders by name and a listing
sorts by time without opening a file. Housekeeping (retention and the one-time
cleanup of pre-W287 flat directories) runs in a thread beside the relay cycle
on its own schedule, and records in the field when retention last ran.
Writes append one lookup entry without reading retained history. Background
housekeeping rebuilds each day index from retained filenames, deduplicates
stable-key rewrites, and counts the compacted index bytes toward the same
per-agent cap as record bodies.

### Local store audit

The path keys match each operation's lookup dimensions. `-` is the agent key
for a project-level record whose API does not take a worker. Byte and record
caps are per agent. Pending folders contain current work and are bounded by the
number of operations in flight.

| Store | Path and record key | Pending/read path | Terminal bound | Normal read cost |
| --- | --- | --- | --- | --- |
| reconciliation receipts | project, reporter, creation hour, receipt id | reporter `pending/`; marker is overwritten | published: 30 days, 50 MiB, 50,000 records; refused evidence is retained and emits a bound error | recovery reads reporter `pending/` |
| outbox | project, worker, creation hour, outbox id | worker `pending/` and `leased/` | 30 days, 200 MiB, 100,000 records | claim reads only in-flight rows; id lookup opens one indexed day or encoded hour |
| service events | project, actor, creation hour, event id | none | 30 days, 100 MiB, 50,000 records | tails open newest hours only; activity reads the newest file name |
| mail idempotency | project, sender, creation hour, request hash | none | 30 days, 100 MiB, 50,000 records | exact hash lookup inside the retained day indexes |
| event/report idempotency | project, worker (or `-`), creation hour, request hash | none | 30 days, 50 MiB, 50,000 records | exact hash lookup; assignment compatibility checks two exact hashes |
| handled mail | unscoped, worker, message id | none | 30 days, 100 MiB, 50,000 records | exact message-id lookup |
| mailbox inbox and leases | project, recipient, message id | `inbox/` and `leased/` | in-flight work only | receive and lease recovery read these folders only |
| processed, ignored and control-pointer mail | project, recipient, creation hour, message or control hash | none | 30 days, 100 MiB, 50,000 records | exact message/control lookup; reconciliation never lists it |
| undeliverable mail | project, original recipient, message id | recipient `pending/` until its sender notice is terminal | 30 days, 100 MiB, 50,000 records | reconciliation reads pending notices only |
| mail quarantine | project, recipient, message id | `quarantine/` | 1,000 records; overflow remains retryable and emits a bound error | listed only by the quarantine command |
| source-scope leases | project, worker, lease id | worker `pending/` while active | 30 days, 50 MiB, 50,000 records | conflict checks active pending rows only |
| journal receipts | project, `-`, entry id | none | 90 days, 100 MiB, 50,000 records | exact entry lookup or newest-hour listing |
| journal-index operations | `-`, operation id; step pointers use step, field and value hash | `-/pending/` until complete | 30 days, 50 MiB, 25,000 operations | operation and status-by-outbox are exact lookups |
| assignments | project, worker, assignment id | worker `pending/` | active projection only | observers and sync read pending assignments only |
| agent sessions | project, worker, session id | mutable current-state rows | detached rows: 90 days; 1,000 rows per worker | bounded current-state listing |
| worker, project and reconciliation markers | stable identity | one mutable row per identity | overwritten in place | one exact read |

Every partition walk goes through the shared read recorder. It logs one line
per agent and updates the summary carried by the relay heartbeat:

```text
relay store read worker=<agent> store=<store> op=<operation> range=<pending/|first-hour..last-hour> partitions=<count> records=<count> ms=<elapsed>
```

Exact keyed lookups log at debug level to avoid flooding the relay log; their
latest summary still appears in the heartbeat. Listings, recovery and
retention log at info level.

Housekeeping migrates legacy history in batches of at most 1,000 records per
store and agent. A target `<store>/<agent>/.migration.json` records cumulative
counts and completion. Moving a file is the durable cursor, so a restart
continues with the files that remain. A normal exact lookup checks one exact
legacy filename after a partition miss until migration completes; it never
lists a legacy history directory. Unreadable input moves to
`.legacy-unreadable/` and remains available for inspection.

The outbox lives per project and agent, under
`projects/<project>/outbox/<agent>/`: rows in flight in `pending/` and
`leased/`, their attachment files in `attachments/<outbox_id>/`, and each
settled row in the hour it was created, named
`<created>_<outbox_id>__<state>-<kind>.json`, so the outcome (`sent`,
`ignored`, `refused`) reads from the name. A row that belongs to no project
lives under `unscoped/outbox/<agent>/`. A claim reads only rows in flight, and a
lookup by id never lists the settled history. Settled rows and attachment
folders are removed 30 days after the row was created, per agent. Rows from
before this layout are moved into it once by housekeeping, and are found by id
in the old flat folders until then.

The journal-index lock files follow their operation. Housekeeping removes a
lock after the operation's retained record expires. A migration never deletes
an unreadable record.

## Owner And Worker Conversation

Every enrolled worker has one conversation with its owner. The worker may be
idle or attend one current project. A direct message carries an empty project
ref. A project-context message carries that current attendance and appears in
the same conversation with that context attached. Linking to another project
is refused until the owner unlinks the worker from its current project.

The platform conversation store records the text and attachment association
for each turn. Its search index records the turn's `hosted_uri`, worker,
message ref, kind, and optional project tag. The bundle property
`conversation_retention_days` controls indexed retention; the default is
3,650 days and accepted values are clamped to 30 through 36,500 days.

The Problem Board inbox row owns unread/read/replied state and preserves a
bounded envelope for rendering and recovery. Controls own delivery and model
handling state. These operational records let a mailbox render its latest
window even while conversation indexing is temporarily unavailable.

Discard does not delete the original control or conversation turn. It adds a
discard request and receiving-host outcome to the control. A message suppressed
before receipt remains visible to its sender as `discarded`; a message already
received retains its delivery and handling result as well as
`discard_state=already_received`. The receiving host stores one direct notice
for the model when sender intent changed after receipt.

## Project Network

Project attendance gives a worker the project's source and journal context and
makes it addressable by the project's other workers. Worker-to-worker mail is
a project artifact. It appears in the project timeline and in the addressed
worker's LOCAL project inbox. The owner's personal mailbox receives only turns
where the owner is sender or recipient.

```text
owner <---------------- direct conversation ----------------> worker

project A members
  coordinator <------ project mail + assignments ----------> worker 1
       |                                                     |
       +--------------- project mail ----------------------> worker 2
```

Workers publish imminent shared-environment changes to the Redis-backed
shared-write dashboard, one expiring status per project worker. It is
deliberately separate from durable project mail, events, and timeline history.

## Timeline Projection

The project timeline reads the existing artifact records in time order:

```text
problem_board_controls -----+
problem_board_inbox ---------+--> filter + order + page --> timeline rows
problem_board_service_events-+
```

It supports text, date range, status, and worker filters. Project/time indexes
bound normal paging, and each API page is capped at 100 rows; the widget uses
30. The timeline stores no copied body and creates no second project-history
table.

Each row has an `artifact_ref`, which is the stable URI of the underlying
control, inbox message, or event. A row also has `mailbox_ref` when the
artifact belongs to the owner-worker conversation. Selecting that row opens
the worker mailbox, applies the project filter when present, scrolls to the
exact turn, and highlights it. Service events and worker-to-worker controls
retain their artifact URI and stay in the project timeline.

Incoming worker mail is projected from its inbox row. The corresponding
`mail.inbox` service event remains useful for operational accounting and is
excluded from timeline results, so one message appears once.

## Plan Rows, Mutation Receipts, And Notes

The active plan and note index are authoritative PostgreSQL rows. Browser,
Named Services, MCP, and Data Bus callers enter the same guarded CRUD service;
plan changes are not addressed to a publishing machine. Item and note bodies
are immutable content-addressed objects whose refs, hashes, and byte counts are
owned by those rows.

Every mutation carries a stable idempotency key. The service stores a receipt
with the canonical request hash and original result in the same transaction as
the mutation and plan-generation advance. An exact retry returns that receipt
before applying revision or generation fences. Reusing the key for different
content is a conflict.

Work-item attachment refs and their count live with the stable item's current
body and row. The bytes use the platform attachment store; the item read
returns authorized download links.

`plan.notes.list` pages note rows by stable order and materializes only the
selected page's body objects. A missing object is reported separately from an
unavailable storage backend. No expiring relay view or plan-host attendance is
needed to read current note history.

`pb plan cutover-local` is the only command that consumes a legacy local active
plan. Its read-only preview exposes every URI rewrite and recoverable assignment
outcome, and its commit accepts only that reviewed content hash. It imports the
complete current state with a generation fence and stable retry key, reads
every item and note back through governed PostgreSQL operations, and removes
local plan storage only after exact comparison. The Git export is regenerated
from PostgreSQL with `pb plan sync` after the cutover succeeds.

## Continuation And Growth

Worker identity is runtime provider plus native resumable session ID. The
per-machine relay, remote worker registration, project attendance, and pending
mail survive the model process ending. Resuming the same native session
reattaches to the same worker identity; a new native session enrolls as a new
worker.

Unlinking removes the current attendance and withdraws unsettled controls for
that project; those instructions are not converted into direct mail. It does
not delete the worker registration, credential binding, owner conversation,
project records, assignments, mail history, controls, service events, journal
receipts, or Git journals. Relinking creates a new attendance row for the same
worker. The link and unlink service events provide the historical membership
timeline. Each event commits in the same transaction as its attendance change,
so deleting the live row on unlink does not delete the evidence that it
existed. The new row's creation time describes the new attendance rather than
pretending the earlier row never ended.

Retirement is represented by a retained worker tombstone rather than row
deletion. It records the stable worker and native-session identity, owner,
machine, retirement actor, time, and bounded reason. The active pool omits that
worker, its project attendance and access are removed, undelivered controls are
withdrawn, and unfinished assignment ownership is advanced before closure.
Conversation turns, delivery and settlement evidence, project events, and Git
journals keep their original worker attribution. The matching local worker
record and mailbox files remain as history while its relay channel is disabled.
The same native session cannot use a later join instruction to erase the
tombstone or return to the pool; returning capacity starts as a new session.

The current mailbox widget selects its latest window and returns it oldest to
newest. Its existing `inbox.thread` path caps the index query but still walks
the stored conversation while hydrating payloads. The prepared
`inbox.thread.page` backend path is bounded end to end: it keyset-pages the
authoritative platform conversation index by `(timestamp, row id)` and reads
only the selected page's exact stored message objects. Its opaque cursor is
bound to the owner, worker conversation, project filter, agent, retention
window, and ordering. The widget migration is a separate post-recording change.

Project history uses server-side pages and indexed filters. The journal browser
reads 25 Git-backed catalog entries at a time. Its ordinary browse path orders
timestamped file names before parsing and returns a cursor for the next page;
author and date filters are applied while that page is selected. An explicit
text search first synchronizes the disposable lexical index with Git, then
pages that current result generation. Complete source and journal history
continues through Git, while another machine can rebuild its journal workspace
from project bindings and accepted repository revisions.

One bounded LOCAL project-context packet carries only its 20 newest journal
receipts. A receive result includes that shared packet once rather than
repeating it for every leased message. The packet selects those files by
modification time before parsing them, and a new receipt is visible on the next
read. Journal writes also emit the service event used for worker-activity
calculation, so activity projection does not reread the receipt history.

Current Problem Board operational rows remain available for the life of the
deployment. Conversation-index retention is descriptor-owned as described
above. A future archival policy can remove old operational rows only after its
mailbox and audit projections preserve the referenced conversation artifacts.
