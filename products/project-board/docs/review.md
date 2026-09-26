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
                                             review.return----+--> todo
                                             review.cancel----+--> cancelled
```

| State | What it asserts |
| --- | --- |
| `todo` | Work has not started. The item may already have an assignee. |
| `working` | Work has started. Working needs an assignee. A released item keeps this status until a status edit changes it. A blocked assignment stays `working` and records its reason separately. |
| `review` | A versioned result is ready for a qualified reviewer. Work completed under an assignment carries its assignment evidence; work managed by a person carries the immutable item version as evidence. |
| `done` | A qualified reviewer accepted the submitted result and its evidence. |
| `cancelled` | Work ended without acceptance. Who cancelled it, when, and why stay on the record. |

## Assignment and status are separate facts

- **Assigning never changes status.** `assignment.assign` records who owns
  the item. The item moves to `working` when the owner reports `working`, or
  when someone permitted to set status sets Working on an assigned item.
- **A status edit changes only the status.** `work.status.set` leaves the
  assignee, the assignment state and the ownership version as they are, for
  every status, Todo and Cancelled included.
- **Ownership moves only by ownership acts:** `assignment.assign`,
  `assignment.return` (release), a reassignment, or a review decision that
  ends the work (`review.accept`, `review.cancel`).
- **The one business rule:** Working needs an assignee.

Why: routing work to an agent says nothing about whether the work has
started, and moving work back to Todo says nothing about who holds it.

In the board's work-item dialog a person edits the Status and Assignee
fields. A save runs two independent steps: a changed assignee is an assign
(or a release, when cleared), and a changed status is a status edit.
Selecting Working requires an assignee; selecting Cancelled requires a
reason. Delete refuses an assigned item, or an item another item depends on.

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

The reviewer is the item's acting assignee while it is in Review. There is
no separate status for it: the item keeps its last worker as its assignee
("worked by") and names a reviewer, an agent or a person. The reviewer is
who must act now.

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

- `operator`: any person in the project. It appears in every person's review
  list, and the first person to accept, return or cancel it decides it for
  all; it then leaves every list.
- `operator:<user id>`: one named person, when that person must look.

A person's review list (Review Assignments) is the items in `review` whose
reviewer is that person or `operator` (`status: review` with
`assignee: @me`), never every item in review.
A review is never left on a person by default. When the coordinator routes
one to a person, it also sends a `decision` mail so the request reaches
their Telegram.

### Who may not review, and what may be required

- **No self-review.** The agent that did the work cannot accept, return or
  cancel it (`work_review_self_forbidden`).
- **Review requirement.** An item's `review_requirement.kind` is `qualified`
  by default, or `operator`, which requires a person. A requirement carried
  on the assignment takes precedence over the item's.
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
| `review.return` | `todo` | kept by the same worker, under a new ownership version |
| `review.cancel` | `cancelled` | settled as cancelled |

Return and cancel take a reason. Each decision is fenced by the item
revision the reviewer read and carries an idempotency key: a retry with the
same key and content returns the same receipt, and a retry with different
content is refused. Every applied decision writes one review record: the
actor, the decision, the reason, the evidence, the time, the item revisions
before and after, and the Card that authorized it.

Refusals are named: a revision conflict, an item not in `review`, an
assignment that has not completed, self-review, or an unmet requirement for
a person.

On the board a person changes the Status field; the record still names the
decision. Review to Done records `review.accept`, Review to Todo records
`review.return`, and Review to Cancelled records `review.cancel`.

## Returns

A return is a decision about the work, not about who holds it.

- The item goes back to `todo` with the reason.
- The same worker keeps the assignment. Its ownership version advances.
- The worker is woken with a returned-work notice naming the new version,
  the review and the reason. It reworks under the new version and reports
  against it, citing the returned-work notice as the source of its report.

Why the version advances: an accepted `completed` report is final for its
ownership version, so the rework needs a new one to report under.

Handing returned work to someone else is a separate act: release it with
`assignment.return` and its reason, then `assignment.assign` to the new
owner. Each step advances the ownership version.

## What done means

`done` says that a qualified reviewer accepted the submitted result and its
evidence. It makes no claim about deployment or runtime state. Where an
acceptance line is about behaviour, the reviewer checks it against the
deployed result before accepting; the status itself does not record that.

A done or cancelled item can be reopened by assigning it again, by someone
whose Card holds `assignment.assign`.

## Where the steps are

- The worker's side (reporting `completed` with `review.look_at` and
  `review.could_not_verify`, and reworking a return):
  [worker skill](../packages/project-board/src/project_board/procedures/problem-board-worker/SKILL.md).
- The coordinator's side (accept, return, cancel, and routing reviews):
  [coordinator reference](../packages/project-board/src/project_board/procedures/problem-board-worker/references/coordinator.md).
- The coordinator role itself: [The coordinator role](coordinator.md).
