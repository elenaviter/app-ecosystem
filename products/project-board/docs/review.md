---
id: project-board-review
title: Work Item Review
summary: The states a work item moves through, who reviews a result and how a review is routed, what a completed report must tell the reviewer, what accept, return and cancel do, and what done means.
tags:
  - project-board
  - review
  - lifecycle
keywords:
  - todo
  - working
  - review
  - done
  - cancelled
  - reviewer
  - review.look_at
  - review.could_not_verify
  - review return
  - ownership version
  - action history
  - project.plan.history
see_also:
  - ./README.md
  - ./concepts.md
  - ./coordinator.md
  - ./operations-by-actor.md
  - ./flows.md
  - ./cards.md
---

# Work Item Review

A result on Problem Board counts only after review. This page describes the
states a work item moves through, who reviews it, what the reviewer is told,
and what each review decision does. The steps an agent follows are in the
worker procedure (see [Where the steps are](#where-the-steps-are)).

## States and transitions

Every work item follows one lifecycle:

```text
todo --working status--> working --assignment.completed--> review
  |                        |                                  |
  +------work.cancel-------+                 review.accept----+--> done
                                             review.return----+--> working
                                             review.cancel----+--> cancelled
```

| State | What it asserts |
| --- | --- |
| `todo` | Work has not started. The item may already have an assignee. |
| `working` | Work has started. The assignee may be empty; assignment and status are independent. A released item keeps this status until a status edit changes it. A blocked assignment stays `working` and records its reason separately. |
| `review` | A versioned result is ready for a qualified reviewer. Work completed under an assignment carries its assignment evidence; work managed by a person carries the immutable item version as evidence. |
| `done` | The item is marked finished. A qualified acceptance is evidenced by its separate review record, not by this field alone. |
| `cancelled` | Work ended without acceptance. Who cancelled it, when, and why stay on the record. |

## Assignment and status are separate facts

- **Assigning never changes status.** `assignment.assign` records who owns
  the item. The item moves to `working` when the owner reports `working`, or
  when Working is set on an item: by an agent whose Card holds
  `work.status.set`, or by any person on the project, admin or member.
- **A status edit preserves the assignee.** Leaving Review for Todo, Done or
  Cancelled records only the ordinary status change, not a review decision.
  It does not settle, reopen or recreate ownership, or issue rework.
- **Ownership moves only by explicit assignee/ownership acts:** `work.assignee.set`,
  `assignment.assign`, or `assignment.return` (release). A review decision may
  settle assignment history. The dedicated `review.return` action routes
  rework to the contributor; it is distinct from selecting a status field.
- **No status-dependent assignee restriction.** Any status can keep an empty
  assignee or receive an explicit assignee edit.

Why: routing work to an agent says nothing about whether the work has
started, and moving work back to Todo says nothing about who holds it.

In the board's work-item dialog a person edits the Status and Assignee
fields. A save runs two independent steps: a changed assignee is an assign
(or a release, when cleared), and a changed status is a status edit.
The field permissions are `work.assignee.set` and `work.status.set`; a combined
save checks those two, with no third permission. A newly selected assignee does
the next work immediately if their existing Card and repository scope allow it.
Selection grants no new authority or credentials and requires no second handoff.
Applied report history continues to name the contributor for review decisions.
Selecting Cancelled requires a reason, but no status restricts the assignee.
Working may be explicitly unassigned; Review-to-Cancelled may select a new
worker. No detour through another status is needed. When both fields change,
one transaction applies the selected ownership and ordinary status fields,
and either all of the save succeeds or none of it does. An ordinary status
edit, including leaving Review for Todo, Done or Cancelled, needs only
`work.status.set`: it records a status change, not a review verdict, and never
settles or reopens the implementation assignment. Dedicated review operations
retain their review authority, no-self-review and audit fences. The dialog always displays
and submits `item.assignee`, for Todo, Working, Review, Done and Cancelled.
Assignment rows and history never replace that field in a projection. Explicit
review routing updates the direct field as described below. The
save resolves legacy aliases and worker ids to the stable worker name only to
compare the selected value with the stored assignee. A status-only save does
not read, derive, clear or recreate the assignee; an explicit assignee edit
sets it once. The worker Card's current assignment list contains the item
exactly when `item.assignee` names that worker.
An explicit clear after a review has closed ownership clears only the item
field and preserves that closed assignment as history. A new selection creates
the new ownership without rewriting the prior ownership
evidence. A person can be selected directly without manufacturing an agent
assignment. The two fields can be saved separately in either order, including
while Review or Done remains unchanged. No extra accept, return or reopen
action is a prerequisite. Agent and person Cards, rails, panels and their
counters have one current-assignee list across all statuses, including Done.
Former contributions never put an item on that list, and the Card offers no
participation-history list or visibility switch. Reassignment moves membership
and counts after the save commits, not while a choice is merely staged.
The chooser marks the actual current coordinator from project-role evidence;
a former coordinator or a suggestive alias does not confer that role.
Its last-implementation hint names the latest contributor with an applied
working, blocked or completed report. Merely routing the next assignment does
not replace that evidence. The hint is historical and read-only, never a
second current owner or an automatic restoration of the previous assignee.
Delete refuses an assigned item, or an item another item depends on.

### Item action history

An item's history is separate from every Card's current-assignee list.
`project.plan.history` reads the recorded work, review, ownership, status and
handoff facts for one item. Each fact retains its stable action ID, timestamp,
action, source reference and the status and assignee recorded at that action.
Historical facts that were not recorded are explicitly unknown. The service
does not infer a past owner from the report author or today's worker alias.
Repeated rounds remain distinct, while a decision's acknowledgment receipt
does not add a duplicate decision. Capture is durable with the original write
and does not depend on retaining its transport notice.

The operation accepts one `work_ref` or `item_key`, a page cursor, and a
`limit` of 1 to 50 (20 by default). The response is
`problem-board.work-item-history.v1`, with total `item_count`, page `count`,
`has_more`, `next_cursor`, and `generation_token`. Pages are newest first,
with a stable action-ID tie-breaker. A cursor belongs to that item, project,
reader, limit and generation. When a committed change invalidates it, the
service refuses with `collection_cursor_stale` and the reader restarts from
the first page. The item panel requests only the selected page, never a
complete ledger to sort or slice locally. Ordinary item reads do not embed
the complete history.
Review actions include the recorded reviewer, decision time, reason and evidence
references in their `review` field. Only the selected page joins these records.
Materialized item reads carry one `latest_review` decision (or null), fetched
with the scoped index and `LIMIT 1`, so brief client output retains the latest
actionable return without a full ledger. A later acceptance supersedes it.

Recorded labels remain separate from optional current `assignee_card`
metadata. That bounded page-scoped target contains only the stable `address`,
identity `kind`, current `label` and `pool_status`, `available` and
`participating`. Availability means identity existence/focusability, not
scheduling capacity or authority. Person identity data comes only from this
project's membership. Clicking an assignee highlights the existing Project
Network Card. A retained retired/off-project identity or another project
person can appear as a read-only identity preview there, without lifecycle
actions, assignment changes or a participation-history list. A deleted identity
keeps its recorded ID and label and explicitly says its Card is unavailable.

## Entering review

An item enters Review when its assignee reports `completed` against the
current ownership version, or when a person sets the status to Review.

Either way, the reviewer must be told two things:

| Field | What it says |
| --- | --- |
| `review.look_at` | Concrete steps the reviewer can perform to verify the result. |
| `review.could_not_verify` | What remains unverified, or an explicit `None` when there is no gap. |

An agent supplies both in its `completed` report, or sets them on the item
(under its current revision) before reporting. A person setting Review
submits both with the status change. A new transition into Review without
either statement is refused, naming the missing field. An item that leaves
Review and enters it again needs a new statement of both.

The report may also name the reviewer (next section).

## Who reviews

Review-routing and decision-authority metadata are stored separately from the
item's current fields. Review is a status, not another current-owner control:
the work-item editor has one Assignee at every status. Explicit
`review.assign`, or a completed report that explicitly names a reviewer,
sets both fields in one transaction and moves current Card membership.
Contributor ownership and `worked_by` remain history. An implicit coordinator
default or migration fills reviewer metadata without changing the assignee.
A later assignee-only edit need not change the reviewer or the status.

### Routing

1. **Named reviewer.** A `completed` report may name the reviewer
   (`review.reviewer`, set with `pb worker report --reviewer`).
2. **The coordinator, by default.** When no reviewer is named, the acting
   coordinator reviews, or the home coordinator when the acting coordinator
   did the work itself. An agent reviewer receives a review request in its
   inbox.
3. **Moving the review.** The coordinator, or a project admin, can move an
   item in review to another reviewer with `review.assign`, under the same
   rules. A person needs `review.assign` on their project Card for this.

The coordinator routes each review in the turn it arrives: it reviews the
item itself when it can check everything the item asks, hands it to another
agent on the project (never the one that did the work), or, when a check
needs a person's browser or account, routes it to a person.

### A person reviews only with integration evidence

A person is named as reviewer only together with the evidence that the work
is integrated:

- `review.integration.merged`: the merged commits, and
- `deploy` (written as `<window>: <check>`), or `nothing_to_deploy`.

Without that evidence the report or the `review.assign` is refused with
`work_review_operator_evidence_missing`, naming what is missing, before
anything is applied. The evidence is kept with the reviewer.

A person is named in one of two ways (the operator's ruling, 2026-09-26:
"operator in the project is any person who works in this project. In
contrary if some specific person is needed then operator:name"):

- `operator`: any person in the project. While it is the current assignee, the
  item appears on every person's current-assignee list. The first person to
  accept, return or cancel it decides the review for all; changing status does
  not remove current membership.
- `operator:<user id>`: one named person, when that person must look.

A person's Card lists items whose current assignee is that person or
`operator` (`assignee: @me` with no status restriction), never every item in
Review or every item they reviewed in the past. Done items remain until an
explicit assignee edit moves them elsewhere or clears ownership.
A review is never left on a person by default. When the coordinator routes
one to a person, it also sends a `decision` mail so the request reaches
their Telegram.

### Who may not review, and what may be required

- **No self-review.** The agent that did the work cannot accept, return or
  cancel it (`work_review_self_forbidden`).
- **An agent decides only as the named reviewer or the coordinator.** An
  agent whose Card holds the review operations accepts, returns or cancels an
  item only when it is the item's named reviewer, the acting coordinator, or
  (with nobody named) the coordinator that reviews by default. Anyone else is
  refused `work_review_not_reviewer`, naming the reviewer and the acting
  coordinator. A person is not limited by this rule.
- **The default worker Card holds the review operations** and
  `plan.item.create`, so a named reviewer records its own verdict and a
  worker files items (Rule 14 of the worker procedure's collaboration page).
  An existing agent Card gets them at its next Refresh, once a project admin
  has ticked them on the project's Control Card.
- **Review requirement.** An item's `review_requirement.kind` is `qualified`
  by default, or `operator`, which requires a person to accept or cancel it
  (`work_review_operator_required` for an agent); its designated agent
  reviewer may still return it. A requirement
  carried on the assignment takes precedence over the item's. See
  [Source approval and final acceptance](#source-approval-and-final-acceptance).
- **Authority is a Card grant.** A reviewer needs `work:review` and the exact
  review operation on its Card. A missing operation is refused with
  `work_review_operation_required`, naming the operation and the Card to
  repair. Making a QA agent a reviewer is therefore a Card edit, not a
  change to the lifecycle. The grants per actor are in
  [Operations by actor](operations-by-actor.md).

## Review decisions

| Decision | Item becomes | Assignment |
| --- | --- | --- |
| `review.accept` | `done` | settled as accepted |
| `review.return` | `working` | rework routed to the contributor, under a new ownership version |
| `review.cancel` | `cancelled` | settled as cancelled |

Return and cancel take a reason. Each decision is fenced by the item
revision the reviewer read and carries an idempotency key: a retry with the
same key and content returns the same receipt, and a retry with different
content is refused. Every applied decision writes one review record: the
actor, the decision, the reason, the evidence, the time, the item revisions
before and after, and the Card that authorized it.

Refusals are named: a revision conflict, an item not in `review`, self-review,
or an unmet requirement for a person. A retired contributor row is not a
prerequisite for editing the item's current fields.

An ordinary Status field edit records `work.status.set`, including Review to
Done, Todo or Cancelled. It preserves the current assignee and assignment
period and writes no review verdict. A qualified review decision requires the
dedicated action and its authority; selecting a status never invokes it.

## Returns

The dedicated return action requests another period of work from the contributor.

- The item goes back to `working` with the reason.
- The same worker keeps the assignment. Its ownership version advances.
- The worker is woken with a returned-work notice naming the new version,
  the review and the reason. It reworks under the new version and reports
  against it, citing the returned-work notice as the source of its report.

Why the version advances: an accepted `completed` report is final for its
ownership version, so the rework needs a new one to report under.

Handing returned work to someone else is a separate act: release it with
`assignment.return` and its reason, then `assignment.assign` to the new
owner. Each step advances the ownership version.

## Source approval and final acceptance

`review.accept` is final acceptance: the item's whole acceptance holds. Source
approval, a verdict that the submitted source and its evidence are right, is
not a board decision. W414 (2026-09-30) showed the difference. Its item was
`qualified`, an agent reviewer accepted the source while the deploy and the
operator's same-profile reauthorization were still outstanding, and the item
went Done.

An item whose acceptance needs a deploy, a live test or the operator's proof is
**operator-final**: `review_requirement` is `{"kind": "operator"}`, set when the
item is created or routed (`plan.item.update`), and kept by every later edit.
Its path to Done:

1. The worker submits. Its `review.could_not_verify` names the outstanding
   proof.
2. The source reviewer gives its verdict on the change request and records it
   as an item note in one findable shape, so the coordinator and the person
   find it without reading prose:
   `Source approved: <repository> <head> [<repository> <head> ...]. Outstanding: <proof>.`
   It does not accept the item: an agent's accept or cancel of operator-final
   work is refused with `work_review_operator_required`. A source defect is
   returned as usual by the designated reviewer (`review.return`, with the
   same reviewer, revision and ownership fences as any return); a return is
   not acceptance.
3. After the verified deploy, the coordinator routes the Review to the final
   acceptor with `review.assign` (`operator` or `operator:<user id>`),
   carrying the integration evidence (merged commits and the deploy check),
   and sends a `decision` mail.
4. The person obtains the live proof and accepts. The item becomes Done.

There is one current Status and one Assignee throughout. Notes and the review
history are evidence, not a second current owner. A worker's `completed`
report is never final acceptance.

## What done means

In the review workflow, `review.accept` records that a qualified reviewer
accepted the submitted result and its evidence, and marks the item `done`.
An ordinary field edit can also select `done`, but the status alone proves
neither acceptance nor deployment. Where an acceptance line is about behaviour,
the reviewer checks it against the deployed result and records that evidence
in the review verdict; the status itself does not record that.

A done or cancelled item can receive a new assignee from someone whose
Card holds `work.assignee.set`, or an explicit assignment using `assignment.assign`.
That ownership act does not change its status;
changing the status is a separate edit.

## Where the steps are

- The worker's side (reporting `completed` with `review.look_at` and
  `review.could_not_verify`, and reworking a return):
  [worker skill](../packages/project-board/src/project_board/procedures/problem-board-worker/SKILL.md).
- The coordinator's side (accept, return, cancel, and routing reviews):
  [coordinator reference](../packages/project-board/src/project_board/procedures/problem-board-worker/references/coordinator.md).
- The coordinator role itself: [The coordinator role](coordinator.md).
