Part of [coordinator](../coordinator.md).

## Confirm that work started

STARTED is the worker's `working` report at the current ownership version.
Queued mail, a settled notice and a heartbeat are not STARTED. Count from the
dispatch receipt (the applied assignment, review routing or send), not from
when the mail was written. With no STARTED 10 minutes after that receipt
(3 minutes for P0 work):

1. Check delivery and the lease once, as in
   [Check a silent worker](check-a-silent-worker-do-not-wait-for-it.md).
2. Recover only when that check shows the dispatch was lost, and only under
   the same identity and key. Never infer that work started, and never create
   a second owner.

A gap in replies is not proof that a worker is offline (W449). Read its
availability ([Refresh the evidence you decide from](refresh-the-evidence-you-decide-from.md)):
an owner whose own state shows it cannot act is handed off by a reassignment
from its checkpoint ([collaboration Rule 8](../collaboration/rule-8-handoff-is-an-ownership-decision-not-a-note.md)), never recovered
by a second owner.

**An unclear assignment is a question, not an idle state.** A worker
reconciles its current assignments with the work it remembers after a resume
or a compaction, once per native wake batch, and periodically while it works
([collaboration Rule 6](../collaboration/rule-6-your-visible-state-says-where-you-are-and-what-you-ar.md), about every 30 minutes, not at every
step). An instruction you put only in an assignment's task is invisible in the
brief item view and the assign notice: state it in the item's description too,
and repeat it in the mail that routes it (W455, 2026-10-01; [Put what a worker
must read](put-what-a-worker-must-read-where-that-worker-can-read-it.md)). When an item stays on it and its
purpose or next action is unclear, it asks you, naming the item, the ownership
version and its last checkpoint. Answer with a durable decision on the item
(the next section): continue, release, reroute or close (W449).
