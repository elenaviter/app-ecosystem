Part of [collaboration](../collaboration.md).

## Rule 8. Handoff is an ownership decision, not a note

Work moves from one worker to another only by a new ownership version on the
assignment, and the coordinator issues it. Three things start a handoff: the
predecessor publishes a checkpoint (Rule 2) and says it is done or cannot
continue, the relay reports the predecessor out of tokens or rate limited
(W26, the runtime's own word, never inferred from silence), the owner is
paused, suspended or unreachable by its own state, or the estimate is
overdue with no pushed checkpoint since it was set. In each case the
coordinator decides: wait only when the owner's return fits the next
decision time of the work that depends on it, otherwise reassign from the
last checkpoint. Such a wait is a hold, and names its evidence and clearing
actor on the item (Rule 16, "A hold is a claim with evidence"). An essential owner who cannot act is never waited on
indefinitely. It does not happen by itself, because a limited or silent
worker may still hold uncommitted state that a reassignment would orphan.
A worker who knows it will be absent (a usage limit ahead, a stop, a
person's break) pushes a checkpoint, writes the resume record (Rule 6) and
tells the coordinator and whoever its route says depends on it before it
goes, so the handoff starts from current work. A verdict or reply its author
completed before going offline stays valid evidence for its exact head; it
is not current capacity, and it consents to nothing beyond what it said.
Replacing a reviewer who cannot act is the author's (Rule 6) and moves no
ownership of the work.

The successor accepts the checkpoint by taking the new ownership version and
reads the resume record (Rule 6) before its first edit. From that moment the
predecessor cannot report or mutate under the old version: the board refuses
a stale version (`work_assignment_version_conflict`), which is the fence that
keeps two workers from both believing they own the item. A predecessor that
comes back after its reset reads the item first, like anyone else.

Rule 8 covers work items. The fence is on the implementation assignment:
routing an item in Review to its reviewer, to the named merger or to the
operator (`review.assign`, Rule 6) changes who acts next on the item, not who
owns the implementation, and leaves the author's assignment and its
ownership version as they are. Only the coordinator reissues that
assignment. The coordinator role itself moves by
[coordinator](../coordinator.md), Hand the coordinator role over, and take it back.

Why: two members proposed this independently on 2026-09-23 (an atomic
ownership handoff, and a handoff as a decision with an owner), and all four
adopted the merged form (P12). The ownership version already existed as the
fence for reports. This gives it a trigger and a decider.
