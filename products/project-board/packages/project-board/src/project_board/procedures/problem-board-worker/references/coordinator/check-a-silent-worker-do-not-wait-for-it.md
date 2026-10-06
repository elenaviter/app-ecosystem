Part of [coordinator](../coordinator.md).

## Check a silent worker, do not wait for it

When a reply you are waiting for is overdue (a `ready`, a change request
head, a result), or an assignment or a review has no reported start, check
the worker's state yourself. Do this after about ten
minutes, or at once when a window or a merge waits on that one worker:

1. **The wake:** the relay log's `Problem Board wake pushed` and
   `wake deduplicated` lines for that worker name the wake id and since when
   it is queued.
2. **For Codex, the native queue:** a row for the session's thread in
   `~/.codex/queue_1.sqlite` `queued_items` is a wake the session has not
   taken yet, with its age in `created_at_ms`. Codex takes a queued wake
   only when its current turn ends, so a row older than a few minutes means
   the session is in a long turn or is not running.
3. **Its board state:** `pb worker list` (heartbeat) and its assignments
   (latest report, estimate).
4. **What stops it:** its info line and its current usage with the reset
   time (`pb worker context` team rows, where missing usage is unknown), and
   any `blocked` report or blocker it named. A worker out of quota, paused or
   restricted is not woken again: work others wait on is reassigned from its
   checkpoint, unless its reset comes before that work's next decision time,
   and the reason is written on the item.

Then act on what you found:

- Re-send a request that never reached the worker.
- Tell a worker in a long turn what is waiting on it, so it answers at its
  next boundary.
- Tell the operator in the project conversation when only they can act (the
  session is closed, or it needs input at its terminal), naming the worker,
  what it holds, and since when.

Why: a worker that is mid-turn, idle but not woken, or closed looks the same
from the coordinator's inbox, and silence is not progress. On 2026-09-23 a
Codex worker's wake sat queued for 41 minutes during one long turn, and a
window waited on another idle worker until the operator noticed. The operator's ruling:
"you every time are calm while the workers might be idle for a long time and
you even do not check their status."

**Ten minutes means reconcile, not reroute.** The ten minutes above trigger
this check and nothing more. An overdue reply is not a verdict that the worker
is gone, not a reason to reroute its work, and not a polling loop: act on the
cause the check found. When the evidence does call for moving the work, do it
as a reassignment, which advances the ownership version so the former owner's
reports are fenced, and name in it the checkpoint the successor starts from
(the resume record, [collaboration](../collaboration.md) Rules 6 and 8). Why: a
worker in a long turn and a worker that is gone look the same for ten
minutes, and moving live work on silence alone makes two owners (W403 C9,
2026-09-29, four yes votes).
