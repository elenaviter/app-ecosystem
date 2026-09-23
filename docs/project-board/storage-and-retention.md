---
id: project-board-storage-and-retention
title: Problem Board Storage And Retention
summary: Where Problem Board state lives, remote and on each machine, how mailbox reconciliation receipts are published, how the owner-worker conversation and the project timeline are kept, and how a worker, its mail and its projects continue across long-running work.
tags: [project-board, storage, timeline, retention, conversation]
keywords: [artifact uri, mailbox link, conversation turns, postgres, local field, git journal]
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

Every host-local mailbox reconciliation run first writes a complete local
receipt, including runs that archive no messages and emit no failure notices.
The receipt records the run interval, reporter identity, directory and archive
counts, every mailbox disposition, each archive result, each failure-notice
delivery and recipient, and every reporting failure with its code and reason.
It records operational metadata and references; message bodies remain in the
private local field.

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
window. A new publication prunes both windows in the same transaction. A local
receipt remains until all of its publication batches are durably sent; only
then may the host apply the 30-day local window.

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
