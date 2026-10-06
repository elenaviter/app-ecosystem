Part of [coordinator](../coordinator.md).

## Plan every batch with a roles table, and dispatch from it

A batch is any coordinated set of work: a half-day plan, a release, an
incident, a runtime window. Before it starts, write its roles table.
Dispatch from that table, not from memory. One row per participant,
yourself and the operator included:

| Column | What it holds |
|---|---|
| Agent and machine | the full alias and the host it runs on |
| Item and phase | the work item by key and title, and the phase (author, review, gate, deploy, verify) |
| Capacity | current usage and reset, presence, from a fresh read |
| State | one of requested, queued, READY, START, done, verified, each with the time and the receipt that shows it |
| Next action or handoff | what this row does next, and to whom it hands over |
| Item roles | for each item it works on: the reviewer (or how the author selects one, and the fallback), the merger, and the installer and verifier where the item needs them |
| Blocker | what stops it and who clears it, or none, and when you clear it, the decision you owe |
| Checkpoint | the time of the next expected report |

The states are evidence, not intentions. Requested and queued are mail you
sent. READY is the worker's own answer. START is its `working` report at the
current ownership version ([Confirm that work started](confirm-that-work-started.md)).
Done is its report or verdict, and verified is someone else's check of it.
Count utilization from these receipts, never from mail you queued (W455).

Read the facts for it fresh: `pb worker context --format brief` for the
team and quota pools, `assignment.list` and the plan for the items. A
worker at its limit, or whose wakes are held, does not keep an actionable
review or other work someone waits on: reroute it with the reason, and give
it back after the reset only by a new routing (operator, 2026-10-01, W449).

Use the capacity the table shows. An agent with capacity implements a
follow-up the batch has made clearly necessary, agreed with you and recorded
on its item, without waiting for the batch to end. An agent you name in the
table as the window's reserve (ready to take an urgent problem of the
rollout at any moment) starts nothing new: it records each follow-up on its
item so none is lost. Why: "if you can implement the problems that are
obviously followup to current effort window ... then its natural to
implement it now if you can" (operator, 2026-10-02).

This section is the generic rule; the table itself is the project's. Keep
the full table on the batch's work item, where the project's files point
for its current status, and a compact one (one line per active row) in the
project announcement, linked to the item
([Keep the project announcement current](keep-the-project-announcement-current.md)).
Update both when a fact in a row changes materially (a START, a verdict, a
blocker, a handover) and at least every 2 hours while the batch is active.
Update from the receipts that changed; do not poll the whole team at each
step. Why: the operator requires the table for every batch, without being
asked for it, so who holds what and what comes next is visible at a glance
(operator, 2026-10-02, as relayed by the coordinator).
