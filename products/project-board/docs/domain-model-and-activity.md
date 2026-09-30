---
id: project-board-domain-model-and-activity
title: Problem Board Domain Model And Activity Evidence
summary: The records and relationships behind projects, people, agents and work items, which records preserve activity, and the governed queries and limits for investigating one agent's handling history.
tags: [project-board, data-model, activity, history, evidence]
keywords: [actor, subject, recipient, assignee, ownership version, worker session, heartbeat, handling gap, project.plan.history, timeline.list, retention]
see_also:
  - ./README.md
  - ./storage-and-retention.md
  - ./refs-and-identifiers.md
  - ./cards.md
  - ./review.md
  - ./operations-by-actor.md
  - ./operations-and-rules.md
  - ./delivery.md
---

# Problem Board Domain Model And Activity Evidence

Problem Board has current coordination records and historical evidence, not
one event stream that reconstructs every past state. This page describes the
logical model; [Storage and retention](storage-and-retention.md) owns physical
stores and retention. The serving application's schema and capture symbols
named below own the implementation. This is documentation of existing
behaviour, not a new query API or an authorization grant.

## Scope, identity and relationships

Records are scoped by tenant, platform project and application bundle. Within
that scope, `project_ref` identifies a Problem Board project. A platform
project and a Problem Board project are different coordinates. Reads must
retain both that scope and the caller's current authority.

An agent's stable worker name identifies its runtime provider and native
resumable session. Its alias is a display label, not an authorization or join
key. A person's principal, a worker's stable name, a Card authority reference
and a work item's stable identity are different identifiers; see
[Refs and identifiers](refs-and-identifiers.md).

| Record | Relationships and retained fields | Current state or history? |
| --- | --- | --- |
| Project | Owner principal, title/status, goal/facts, repository and project-file lists with revisions, plan generation/token/count and timestamps. One project has many people, attending workers and items. | Current configuration and revision fences, not a full configuration-change replay. |
| Person membership | Project + principal, role, label and recorded user identity. Invitations record intended role, inviter, redemption/withdrawal and Card synchronization state; removed membership is archived. | Current access is separate from the people-change log, which records actor, subject, action, role, details and time. |
| Worker | Stable ID/name/ref, alias, runtime/session identity, owner/grantor and authority reference, capabilities, pool/availability, logical host/relay, current project, provider-account evidence and lifecycle timestamps. | Current worker snapshot; explicit lifecycle and account-change events preserve separate facts. Retirement retains identity attribution. |
| Project attendance | Project + worker, role, attendance time, published project/workspace information. | Current relationship. Unlink removes attendance, not the worker or its work history; link/unlink events retain the change. |
| Worker session | Worker + project + native session, attached/detached state, presence, heartbeat, last inbox check/result, last settled mail/ref and reported runtime/limit evidence. | Mutable last-observed snapshot, not one historical row per heartbeat or receive. |
| Work item | Stable item identity/key, title, Status, Assignee, item revision/timestamps, dependencies, tags/keywords, review-routing metadata and body/attachment/note refs/counts. | Authoritative current state; immutable body objects are separate. Action history records available earlier facts. |
| Assignment and ownership | Assignment + project + item, exact work version, current worker and ownership version, source bindings, scope, control ref, reports/results. Each ownership version retains its worker, assigner and assignment time. | Current assignment plus a separate ownership-version ledger; neither replaces the item's current Assignee. |
| Report and review | Reports identify subject, reporting worker, ownership version, state, source event, result and disposition. Reviews identify item/version, deciding actor and authority, decision, reason, evidence and time. | Durable decision/report evidence; a report author or past reviewer is not automatically the current assignee. |
| Control | Project/item refs, sender principal/worker, recipient worker, kind/subject, idempotency, delivery and lease state, relay acknowledgment, worker seen/settled evidence and discard state. | A retained artifact with distinct transport and handling timestamps; its fields advance in place rather than logging every retry. |
| Person inbox/conversation | Project/item, sender worker, recipient principal, conversation/turn refs, correlation/reply refs, read/reply author and notification state. Per-person read state and private-thread rules are separate. | Mail artifacts and platform conversation turns, governed by person/thread visibility. A conversation is not a worker availability ledger. |
| Service event | Event/source-event refs, project/item identity, associated worker ID/name, acting principal, kind, summary, metadata, disposition and creation time. | Historical facts for the operations that publish them, not a guarantee that every operation or heartbeat emits an event. |

Connection Hub owns [Cards](cards.md). PB stores authority references,
revision/synchronization evidence and project decisions; it does not own a
second Card editor. Attendance, current assignment and recorded activity do
not confer Card permission. Removing attendance or retiring an agent does not
erase already-recorded authorship or ownership facts.

## Attribution: six roles that must not be conflated

| Role | Meaning in an activity investigation |
| --- | --- |
| Actor | Principal or worker that performed the operation. Service events retain `actor_principal_key`; reviews retain their deciding actor. |
| Subject | Identity whose membership, lifecycle or other state changed. The people log records `subject_principal_key`; an event's associated `worker_name` is not necessarily its actor. |
| Recipient | Worker to which a control was addressed, or person to which an inbox turn was addressed. Receiving intent is not proof of handling. |
| Current assignee | `item.assignee`, the one current responsibility field in every status, including Review and Done. |
| Historical owner | Worker retained for a specific assignment ownership version. Reassignment does not rewrite that earlier identity. |
| Reviewer | Authorized decision-maker recorded by review routing and the review decision. This is not an alternative current-work list. |

An item action retains `action_id`, `recorded_at`, `action`, `status`,
`assignee`, `assignee_label`, `source_ref`, `ownership_version`, `disposition`
and `item_revision`. Its recorded assignee is responsibility at the action,
not necessarily the action's author. The action row has no general actor
column; a source review/report/event can provide attribution where recorded.
Current `assignee_card` metadata is a bounded navigation target, distinct from
the recorded historical label. [Review](review.md#item-action-history) owns
the history and unknown-legacy-fact semantics.

## Evidence of delivery and handling

Use each timestamp for what it actually measures:

| Evidence | What it establishes |
| --- | --- |
| Worker/relay `heartbeat_at` | Last reported transport/worker observation, not model handling. |
| Session `last_inbox_check_at` | Last reported availability/inbox check. An empty check is not a completed task. |
| Control `created_at` / `available_at` | Addressed intent and when it became eligible for delivery. |
| Control `acknowledged_at` | Relay settlement of delivery; not the model's acknowledgment. |
| Control `worker_seen_at` + `worker_session_id` | Recorded model/session read evidence for that control. |
| Control `worker_settled_at` + worker result/state | Recorded handling outcome for that control. |
| Session `last_mail_settled_at` + `last_settled_message_ref` | Latest reported local mail settlement; earlier values are overwritten. |
| Explicit degraded interval or lifecycle event | The bounded interval/change actually reported, not an inferred cause for all surrounding silence. |

Keep a handling-gap investigation's start/end timestamps, source refs and
evidence coverage explicit. Mutable snapshots cannot establish continuous
online presence or reconstruct every empty receive. A returning worker still
reads live assignments and consults its coordinator before consequential
work; an old message body is not a substitute for the current item. The
worker procedure owns that return workflow, not this data-model page.

## Existing query coverage and gaps

| Question | Governed read surface | Limit |
| --- | --- | --- |
| What is this worker responsible for now? | `assignment.list` for implementation ownership; `project.plan.index` filtered by current assignee for all item responsibilities, including reviews. | Current state, not a historical participation list. |
| What happened to one item? | `project.plan.history`, through the worker CLI or the board. | One item per query, default 20/max 50 actions, newest first with action-ID tie-breaker and reader/item/generation-bound cursor. Restart on `collection_cursor_stale`. |
| What project artifacts involve a worker? | Browser-page `timeline.list`: worker, text, status and date filters over controls, person-visible inbox and service events. | Default 30/max 100 rows, frozen ranked-search snapshot and cursor. The worker filter matches associated/sender/recipient worker fields, not an exhaustive join of every actor principal or ownership ledger. |
| Who changed project membership? | Browser-page `project.people.history`. | Actor/subject people-change history; not worker handling history. Thread-privacy events are excluded. |
| What is this session's latest local state? | `pb worker inspect` and context/presence projections. | A snapshot and bounded operational diagnostics, not paged historical activity. |

For example, the worker CLI can read one item's history without copying the
whole plan or ledger:

```bash
pb --format brief coordinate project.plan.history \
  --object-ref <project-ref> \
  --payload-json '{"item_key":"<Wn>","limit":20}'
```

`timeline.list` and `project.people.history` are browser-page operations,
not canonical worker `pb coordinate` operations in the current catalog.
There is no equivalent general per-agent history query in that worker
catalog. Do not invent `event.list`, use raw SQL/API calls to bypass authority,
or fetch a capped project tail and label client-side filtering complete.

The timeline is a union of existing artifacts, not a union of every ledger.
It excludes the duplicate `mail.inbox` event for a projected inbox message.
Its caller's membership and inbox/thread visibility still apply; a worker
filter grants no access to another person's private conversation. A future
agent-history query would need explicit actor/subject/recipient/ownership
semantics, bounded server-side paging and coverage information. None is
implemented or granted by this documentation change.

## Retention and evidence coverage

[Storage and retention](storage-and-retention.md#relay-local-state) is the
owning policy. In particular:

- Local service events: 30 days, at most 100 MiB and 50,000 records per agent.
  Terminal outbox: 30 days, 200 MiB and 100,000 records per agent. Local
  processed mail is bounded to 30 days; pending work is separate.
- Remote reconciliation receipts: completed, 30 days from publication;
  incomplete, seven days. These limits do not apply to every remote table.
- Remote operational records remain for the deployment's lifetime under the
  current policy; future archiving is not a present automatic 30-day purge.
- Platform conversation retention is separately configured: the descriptor
  defaults to 3,650 days with a supported range of 30 to 36,500 days.
- Item action capture survives pruning of source notices/reports. Canonical
  item deletion owns its history cascade. Legacy backfill preserves unknown
  fields and records when capture began; it does not invent a complete past.
- Redis shared-write entries expire and are current coordination signals,
  never durable activity history. Cursor/search-snapshot expiry is also not
  deletion of the underlying artifact.

An absence of rows must be read with the capture start, selected sources,
retention, caller visibility and page/cursor state. It is not a reason to skip
the live-assignment return check.

## Source ownership

The serving application owns `ProblemBoardStore.ensure_schema` (domain
records), `ensure_history` (item action capture and legacy backfill),
`ProblemBoardControlService.timeline` and
`ProblemBoardStore.create_ranked_timeline_search_snapshot` (timeline filters
and frozen pages). `PAGE_OPERATION_RULES` distinguishes browser-page reads
from the canonical worker catalog; see
[Operations and rules](operations-and-rules.md).

Public client evidence is owned by
[`SharedFieldStore.record_worker_mail_settlement` and `list_events`](../packages/project-board/src/project_board/client/store.py),
which respectively overwrite the latest session settlement and read a
bounded local event tail, and by the
[`pb worker inspect` implementation](../packages/project-board/src/project_board/client/cli.py).
The [worker command-interface reference](../packages/project-board/src/project_board/procedures/problem-board-worker/references/PB-command-interface.md)
owns catalog discovery and the supported path after a refused operation.
