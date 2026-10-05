---
id: project-board-storage-and-retention
title: Problem Board Storage And Retention
summary: Where Problem Board state lives, remote and on each machine, how mailbox reconciliation receipts are published, the rules that keep each relay's local state bounded, how the owner-worker conversation and the project timeline are kept, and how a worker, its mail and its projects continue across long-running work.
tags: [project-board, storage, timeline, retention, conversation]
keywords: [artifact uri, mailbox link, conversation turns, postgres, local field, git journal, relay local state, retention, pending folder, hour partition, cold tier, event archive, mail archive, hot_days]
see_also:
  - ./README.md
  - ./domain-model-and-activity.md
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

[Domain model and activity evidence](domain-model-and-activity.md) maps the
project, participant, item and delivery relationships, separates mutable
presence snapshots from historical facts, and states which governed queries
can investigate one agent's activity.

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
relay store read worker=<agent> store=<store> op=<operation> range=<pending/|first-hour..last-hour> partitions=<count> records=<count> ms=<elapsed> [project=<project>] [key=<id>] [removed=<count>]
```

The trailing fields name the store's other dimensions when they apply:

- `project=`: the project whose tree was read (a store under `projects/<id>/`).
- `key=`: the id a lookup asked for (a record, an assignment, a lease).
- `removed=`: records a retention pass removed, for the age and size bounds together; always present on `op=retention`.

The heartbeat's `store_reads` summary carries the same fields. It rides on the
next project heartbeat when the last read of a store changed; a read that
differs only in `ms` or `at` is not sent again.

Reads the relay makes on every cycle or wake log at debug level: exact keyed lookups and in-flight folder listings (`lookup`, `pending`, `pending-list`, `leased-list`, `lease-recovery`). At info they flooded the rotating relay log, lookups on 2026-09-24 and the outbox in-flight listings at the W287 switch (about 1,100 lines a minute). Their latest summary still appears in the heartbeat. Listings of history, startup recovery and retention log at info level.

Two tests in `tests/test_relay_local_state.py` guard startup and the cycle
against a large history: one over 50,000 flat pre-partition records, one over
50,000 records already in hour folders. Each asserts no history folder is
listed and the time stays bounded.

A refused `mail.reconciliation.publish` row keeps its receipt under `refused`.
When the refusal is a Card whose operation list predates the operation
(`work_worker_operation_not_granted`), the settled row's `remote_result` also
names `permission_group`, `why` and the `fix`: a project admin presses
Refresh worker Card on the agent's row in Team > Agents (Refresh coordinator
Card for the coordinator), which keeps the same Card, client and session
(W420). The relay row counts these
as `reconciliation_publications_refused`.

A refused receipt is published again once the Card holds the grant
(`client/reconciliation_replay.py`). The relay cannot read the Card, so
housekeeping asks the service:

- **Probe.** At most once an hour per project and agent, the oldest refused receipt is queued again. A refused probe is quiet: a `relay store replay … op=probe outcome=refused` log line, and no board notice.
- **Batch.** A published probe proves the grant. Up to 100 refused receipts are then queued again on each pass, until none is left.

Replayed batches get new outbox ids (`<content id>_r<n>`), because the content id already names the refused row. Batches the service accepted keep their ids. The per-agent marker `replay.json` keeps the probe's state across restarts, and is removed once nothing refused is left.

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

Mail attachments between workers use the same Board-backed storage as
operator uploads. The sender snapshots each file into its own outbox,
including a byte size and SHA-256; equal display names have separate snapshot
directories. The relay uploads the bytes, and the Board stores them on a turn
in the recipient's conversation before committing the control with a signed
download manifest. The receiving relay verifies and materializes its own copy;
no sender-local path travels in the Board envelope. A content-bound retry
reuses the committed control and links instead of creating another message.
Staged mail inputs remain subject to staging expiry so an outcome-unknown
retry can use the same upload or an identical newly staged file.

`worker forward` requires the original message's exact unexpired session
lease and verifies its local file manifest before creating a new outbox
snapshot. It records the original message, sender and kind as provenance,
not as authority. Settling the original does not erase either retained copy.
Both mailbox lanes allow ten files of at most 25 MiB each and enforce the
shared `no-executable-binary` content rule; source and scripts remain allowed.

The journal-index lock files follow their operation. Housekeeping removes a
lock after the operation's retained record expires. A migration never deletes
an unreadable record.

### Disk and workspace size

Finished worktrees filled a host disk before anyone saw it (2026-09-30). The
relay's heartbeat carries `disk_usage`: the host's free and total bytes for
the file system that holds the agent's workspace (one `statvfs` per beat) and
the workspace's own size. The size walks the tree, which takes tens of seconds
on a large workspace (time follows the file count more than the bytes). So a
background task re-measures it at most every 15 minutes, in a child process,
with at most two walks at once per relay, and the heartbeat never waits for
it. Two slots, not one, let the other channels' walks pass a long one at
relay startup: each beat carries the last
measured size, or none until the first walk lands (the card then shows
"measuring"). The board keeps the latest report per
agent, shows it on the agent card ("disk … free (…%) · workspace …") and in
the project team context, and drops a malformed report with a
`worker.disk_usage_dropped` event instead of failing the heartbeat.

When a host crosses below the alert threshold (default 10% free; the board's
bundle property `disk_alert_free_percent` changes it), the operator gets a
`decision` mail and the acting coordinator a mail naming the machine, its
largest agent workspaces and `pb worker workspace --sweep`. One alert per host
crossing, however many agents run there: the state is kept per host, and a
report above the threshold from any of its agents re-arms it.

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

The platform keeps the most recent window of that index hot and moves older
rows, embeddings included, to a verified cold tier in bundle storage; a
date-filtered read reaches them by time (KDCube conversation retention). The
window is the assembly property `routines.conversation_store.hot_days`
(default 90 days), and the platform's `conversation-archive` job moves rows
once a day at 02:20 UTC; `routines.conversation_store.archive_enabled: false`
turns it off. Archived rows are written per UTC day under
`conversation-cold/<yyyy>/<mm>/<dd>/` with a manifest holding each part's
SHA-256, and are deleted from Postgres only after the part is read back and
checked. KDCube's conversation list and an opened conversation still include
archived messages within their own rolling window.

Unlinking and relinking keep the conversation. The agent's owner may choose,
on unlink, to delete their own messages with the agent in that project, or, on
retirement, their whole conversation with it (`delete_conversations: true`).
Other people's exchanges with the agent stay. The choice
is offered only to the owner and checked before anything changes; the platform
deletes the hot rows, the archived records and the stored bodies, and records
the deletion with its actor, time, scope and counts. A failed deletion never
undoes the unlink or retirement and is reported in the result; asking again
with the same choice runs only the deletion, also for an agent that already
left the project.

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
30. The timeline creates no second project-history table.

A search ranks the complete match set once and keeps that ranking as a
snapshot, so later pages follow the same order and `matched_count` is exact:

- **A snapshot holds ranks, not copies.** Each matched entry is one rank row in
  `problem_board_timeline_search_snapshot_items`: rank, `artifact_ref` and the
  search scores. A page rebuilds its entries (at most 100) from the live rows
  by ref, under the same project scope and inbox visibility as the ranking. An
  archived event has no live row, so its rank row keeps the entry itself.
- **A page whose row is gone** (deleted, or no longer visible to the caller)
  reads as incomplete, and its cursor answers `collection_cursor_stale` (409).
  A new search starts again.
- **Snapshots expire** 15 minutes after the search. The board's
  `timeline-snapshot-sweep` job deletes expired ones every 10 minutes, one
  instance per tenant and project, and each search deletes them too.
  `enabled.cron.timeline-snapshot-sweep: false` turns the job off.
- **One person keeps at most three** live snapshots per project: a new search
  deletes their older ones, and a cursor into a deleted snapshot answers 409.
- **An open search is not re-ranked by board activity.** The Timeline keeps its
  snapshot and page while the board polls; activity after the search was
  ranked shows "New activity since this search" with a Refresh, which re-runs
  the same search from page 1. Search, Reset or a project change re-rank.

The snapshot tables are a cache. Emptying them loses no data; open cursors
answer 409 until the next search.

Each row has an `artifact_ref`, which is the stable URI of the underlying
control, inbox message, or event. A row also has `mailbox_ref` when the
artifact belongs to the owner-worker conversation. Selecting that row opens
the worker mailbox, applies the project filter when present, scrolls to the
exact turn, and highlights it. Service events and worker-to-worker controls
retain their artifact URI and stay in the project timeline.

Incoming worker mail is projected from its inbox row. The corresponding
`mail.inbox` service event remains useful for operational accounting and is
excluded from timeline results, so one message appears once.

## Service-Event Archive

Service events older than the same hot window as the conversation index
(`routines.conversation_store.hot_days`, default 90 days) can move from
Postgres to the board's bundle storage. The board's `event-archive` job runs
once a day at 02:40 UTC, one instance per tenant and project:

```text
<board bundle storage>/events/<project-id>/<yyyy>/<mm>/<dd>/
  <batch-id>.jsonl.gz        the day's events, one JSON record per line
  <batch-id>.manifest.json   row count, event ids, time range, SHA-256
```

Each batch is recorded in `problem_board_event_archive_batches` before
anything is deleted. The job reads the part back, checks its SHA-256 and its
exact event set against the manifest, and only then deletes those events and
marks the batch `pruned`. A failed check deletes nothing and records the error
on the batch; an interrupted run resumes from the ledger.

Some events stay in Postgres whatever their age:
- `worker.retired`, because the Archive reads a retired agent's history from it;
- each agent's latest tooling notice of each kind, which its Card shows;
- each agent's latest runtime-account change, which every heartbeat replays.

A timeline search whose date range starts before the newest archived event
also reads the archived days in that range. A search without a start date
reads Postgres only.

## Mail Archive

The same daily job also moves inbox mail (`problem_board_inbox`, mail an
agent sent a person) and controls (`problem_board_controls`, mail a person or
an agent sent an agent) older than the hot window to bundle storage. The
steps are the same as for events: write the part, read it back and verify
it, and only then delete the rows. Each batch is a row in
`problem_board_mail_archive_batches`. One part holds one project's, one
conversation's (one agent's) and one UTC day's messages, each record a
whole row:

```text
<board bundle storage>/mail/<project-id>/<agent>/<yyyy>/<mm>/<dd>/
  <batch-id>.jsonl.gz        the day's messages, one JSON record per line
  <batch-id>.manifest.json   row count, message ids, time range, SHA-256
```

A message stays in Postgres while anything still acts on it:
- mail its person has not read yet;
- a control still pending, leased, or with a discard requested;
- the current control of an active assignment;
- retirement delivery evidence;
- each agent's latest notice of a kind its heartbeat replays.

A message and the control that answered it move together. A row that
changed after its part was written is not deleted. Assignments and their
ownership history are the board's current state and are not archived by age.

An archived message leaves a small index row in Postgres
(`problem_board_inbox_archived`, `problem_board_controls_archived`). It holds
the message's identity, its dedupe key, the fields that decide who may read
it, and its time; the body and payload are in the archive. With that row:
- a resent message is still answered as a replay, so there is no second row
  and no second Telegram post;
- a reply to an archived control still finds its sender;
- thread counts and the Inbox's dated worker search include archived mail.

Reading archived mail back:
- A dated timeline search reads the archived days in its range, like events.
- An Inbox conversation pages its live and archived messages in one order.
  Unread mail stays live past the window, so the two interleave in time.
- Both mark an archived row `storage: cold`; the board shows it as
  **Cold archive**.

The bundle property `enabled.cron.event-archive: false` turns the job off.
Deleting rows makes their space reusable for new rows; it does not shrink the
table file, which only `VACUUM FULL` or an equivalent rewrite returns to the
disk.

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
body and row. The bytes use the platform attachment store. No download link is
stored or returned by a read: `work.attachment.link` issues one per download
(W485, [delivery](delivery.md)).

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

## Runtime-Window Database Backups

A runtime window's database backup lives in one folder per host and project,
`<backup root>/<project id>`, readable only by the host user. The operator sets
the root with `pb host configure --backup-root <path>`. It must lie outside
every Git working tree, and until it is set nothing is created. A
`manifest.json` in the folder lists each backup the coordinator recorded: the
file, its format, size and SHA-256, a label, who recorded it, and its check.

| Command | What it does |
| --- | --- |
| `pb worker backup --project-ref P --new --dump-format F` | prints the file to write the next backup to (coordinator) |
| `pb worker backup --project-ref P --record FILE --dump-format F --label L` | checks the file and adds it to the manifest (coordinator) |
| `pb worker backup --project-ref P --prune --all-clear E [--apply]` | after a verified ALL CLEAR, keeps the newest backup and deletes the older manifest entries (coordinator) |
| `pb worker backup --project-ref P` | lists the backups, newest first, and files the manifest does not list (anyone) |

The check depends on the format:

- **`plain-sql-gzip`** (`pg_dump | gzip`): the whole gzip stream is read, so its
  CRC and length are checked. The dump must carry pg_dump's header, its
  completion marker (a truncated dump has none) and at least one table.
- **`pg-custom`** (`pg_dump -Fc`): the `PGDMP` header is checked, and
  `pg_restore --list` must read the table of contents.

Both record `restore_proof: false`. Only restoring into a scratch database
proves a backup restores.

Pruning refuses without the ALL CLEAR evidence, and refuses when the newest
backup failed its check or no longer matches its recorded hash. It deletes
only files the manifest lists, and never a symbolic link.

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
machine, retirement actor, time, and bounded reason, and where revoking the
session's own Connection Hub Card stands (`card_revocation_state`: `pending`,
`revoked`, `failed` with the reason and attempt count, `no_card`, or empty for
a worker retired before this was recorded). The active pool omits that
worker, its project attendance and access are removed, undelivered controls are
withdrawn, and unfinished assignment ownership is advanced before closure.
Conversation turns, delivery and settlement evidence, project events, and Git
journals keep their original worker attribution. The matching local worker
record and mailbox files remain as history while its relay channel is disabled.
The same native session cannot use a later join instruction to erase the
tombstone or return to the pool; returning capacity starts as a new session.

Retired workers stay readable in **Workpool > Archive** (`workers.archive`): a person sees their own retired agents and a platform admin may ask for everyone's. The board filters by alias or name and retirement date, pages one list with a cursor, or returns the last N per machine or per project the agent attended, so the browser never loads the whole retired population. Asked with `worker_name`, it returns one read-only archived card (retirement, host, runtime, provider account history, Card revocation, the projects attended and the last assignments), which is where historical attribution links resolve. Only projects the viewer is a member of are named, on the card and as project groups; the rest are counted, never named, each agent once however many of those projects it attended. Grouped reads return at most 50 groups, and a searched `%` or `_` is literal text.

A worker retired before its Card was ended by retirement is found by `workers.card_reconciliation`: it proposes the retired agents whose session Card the board has not ended, with a confirmation token for the exact selection, and revokes only when that token comes back for exactly those agents. Only an agent's owner can confirm: Connection Hub revokes a Card only as its owner, so a platform admin's everyone view shows the proposal read-only. When Connection Hub reports no active Card of the owner with that id, the board asks it, as the owner, for the owner's live Cards: a Card absent from that list is ended and recorded `revoked`. Otherwise the state is `not_active`, which is not proof the board revoked it and does not unlock deletion; the owner resolves it by retiring the agent again or through reconciliation, which proposes it again. Permanent deletion (`worker.delete`) is a separate, explicit action, never part of retirement: the owner types the agent's name and "delete permanently", and only a retired agent whose Card is revoked, or that never held one (including one retired before this was recorded), qualifies. The row stays as an attributable tombstone (stable name, alias, ref and identity; provider account and store reads cleared), is left out of the Archive, and a historical link to it shows the tombstone, opening no card. Reconciliation and deletion each record an audit event.

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

Controls, inbox rows and plan rows remain in Postgres for the life of the
deployment. Conversation-index retention and the service-event archive are
described above. Both use the platform's one hot window and the tenant and
project's bundle storage. Archiving message bodies is not built yet. Whether
it uses that same window or a board property of its own, and which key layout
it uses, is still to be decided. A future archival policy can remove old
operational rows only after its mailbox and audit projections preserve the
referenced conversation artifacts.
