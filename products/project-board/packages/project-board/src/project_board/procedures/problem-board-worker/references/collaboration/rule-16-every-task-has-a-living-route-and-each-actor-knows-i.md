Part of [collaboration](../collaboration.md).

## Rule 16. Every task has a living route, and each actor knows its next step

Every task carries, on its item, a route from planning to acceptance that
every participant can read, kept whole through every reprioritization: the deliverable and its scope, the acceptance,
the current actor and its next action, where reports go, the independent
reviewer (or how the author selects one, and the fallback), the named
merger, the installer and verifier where the task needs them, the current
blockers with who decides each, and the handoff after each step. The default route is: analysis and implementation,
exact-source and test evidence, direct independent review, the named merger,
installation and live verification where needed, then acceptance. The route
may change as the work does. The item's assignee always shows the actual actor,
and its route the actual step. Each actor who finishes a step records its evidence
and the next handoff on the item, and hands the item to the next actor the
route names itself, the operator included (`assignment.assign`,
`review.assign` or `work.assignee.set`, and the status when it changes), in
the same act as asking them and without waiting for the coordinator: a mail
moves nothing. The next actor reads the route there, not in old mail; whoever holds an item
whose route is missing, or no longer fits after a reprioritization or a
reassignment, completes the route on the item at once. A missing technical detail never leaves the
route without an owner or waiting on an acknowledgement: the route names who
finds it out.

| Role | Responsible for |
|---|---|
| Coordinator | Tracking assignments, actual progress and live availability where they decide something, by reading the items, reports and team state (a bounded pull), not by asking for status mail; seeing that every route names its current actor and next action, which each holder keeps; readable instructions that are consistent, with superseded ones marked; handing off a work owner who cannot act, by a reassignment from its checkpoint (Rule 8); the batch roles table; the decisions in point 2. Nothing below transfers this. |
| Author | Starting at once; one consolidated clarification; implementing within the bound scope; exact-source and test evidence; arranging its review and replacing a reviewer who cannot act (Rule 6); a visible next action and checkpoint; a checkpoint and a notice before a known absence (Rule 8). |
| Reviewer | Verifying the exact changed source and proportionate test evidence; a verdict at the exact head; handing the item to the named merger (Rule 6). |
| Merger | Merging an exact head with an independent PASS and the named gates without another routine acknowledgement, reusing unchanged evidence, proving the merged tree (Rule 5); handing on to the installer or verifier. |
| Installer and verifier | Making the merged work live where the task needs it, verifying it, and recording the receipts on the item for acceptance. |

The coordinator reference applies this route to dispatch ([coordinator](../coordinator.md),
"Record each dispatch on the item"), and the project's files and roles table
hold the actual names; this procedure holds only the generic rule.

**A hold is a claim with evidence.** A hold is anything that stops or defers
an item's next step: a gate on a merge, a deployment or an activation, a
blocker named on the route, a reviewer keeping a review, a window
participant's HOLD. It stands only while it names, on the item:

- the **requirement or resource** the held step would affect (a contract, a
  schema, a running counterpart, a host, a credential) and the **evidence**
  that it would: a failing check, a version or field the other side has not
  shipped, a resource both steps change;
- the **actor or event that clears it**, the **smallest action** that does,
  and the **checkpoint** when it is decided again.

A hold that cannot name these is not a hold, and the step goes ahead, subject
to its other gates. A window is the exception that keeps its gate: a
participant that answers HOLD without naming its operation is **not READY**,
and a host window still starts only with every affected session's READY or
evidenced quiescence (Rule 10); it is never started over an unexplained
HOLD. These never make one: deferred or future hardening the step does not depend on; a
window on another host, or one the step's own runtime does not take part in;
proximity (the same files, the same day, the same people, a related item).
A real compatibility dependency is one: the held layer reads a contract or
schema field the other has not shipped, or the running counterpart would
refuse it. A runtime-only release is held only by an operation in flight it
would conflict with; a host client switch or relay restart also needs the
host window's quiescence (Rule 10). A doubt about whether a dependency is real
is settled by one bounded check with the owner of the other side, one
question and one reply by a stated time, not by waiting. A demonstrated
authority or safety conflict is a **stop**, and stays one until the actor it
names clears it. The coordinator decides a hold and keeps it current on the
item; a worker who sees a real conflict flags it with its evidence; when a
hold is contested, an available non-author reviewer checks what the gate
actually guards. The review case is Rule 6 ("Unmet criteria go back"), the
window case Rule 10, and the cross-layer case the coordinator's "Deliver a
cross-layer item". Why: on 2026-10-04 a deployment was held behind deferred
permissions hardening that it did not depend on, with no requirement,
evidence or clearing actor named, and only the operator's correction released
it (W541).

What the author does to carry its part:

1. **Start at once, and clarify in one round.** Read the item, its route and
   the project files, then send every initial question together, in one
   message to the coordinator, or to the operator when the question is about
   behaviour the operator has not decided. Keep working on what the questions
   do not block.
2. **Escalate once, with options, only what is genuinely new.** Material
   scope beyond what the binding authorized, new authority or permissions, a
   safety question, a deployment coupling, or a conflict with someone's
   unpublished work goes to the coordinator once, naming the options and what
   you recommend. Implementation and test choices inside the bound scope,
   choosing and replacing your reviewer (Rule 6), a merge by the named merger
   after the named gates, and recovering your own disposable test fixtures
   are yours, with no approval asked.
3. **Read availability now, at each point it decides something.** Before you
   ask someone for a review, a reply or readiness, before you list who a poll
   or a window waits on, and again when you interpret what came back, when you
   take over a handoff, when you choose your next action, and when you learn
   that someone's availability changed. Availability is current state read
   together: the work the teammate holds and its latest report
   (`assignment.list`), its busy-until and info line, whether its session is
   reachable and listening, and its provider's usage limit and reset
   (`pb worker context`, `pb worker list`), as the coordinator reference
   defines it ([coordinator](../coordinator.md), "What the coordinator is for").
   An inbox with nothing pending, an idle label or an earlier read is not
   availability. Silence is never consent, approval or READY.
4. **An unavailable reviewer is yours to replace; an unavailable work owner
   is the coordinator's to hand off.** When your reviewer cannot act by your
   next decision time, ask the next qualified available teammate yourself
   (Rule 6). When someone else's step your work needs (a merge, an install, a
   dependency's change, a window's execution) waits on an owner who cannot
   act, tell the coordinator what you read and when, and carry on with what
   does not depend on it: that handoff is the coordinator's (Rule 8).
5. **The item is the status; mail goes to whoever acts next.** Progress,
   receipts, evidence and acknowledgements go once, on the item (complete logs
   in an attachment or a file it references), and the item is what everyone
   reads. A handoff is one actionable mail to the named next actor (your
   reviewer, the merger, the installer), not a copy to everyone. The
   coordinator gets mail only for: a genuinely new scope, authority or safety
   decision; a blocker you cannot resolve with the actor in front of you; or a
   ready result whose next step is the coordinator's own (an activation, a
   routing only it can make). Each names the item, its revision, the exact
   head and the time, and points to the evidence on the item rather than
   repeating it. Never: a routine acknowledgement, a "still waiting" or
   progress mail, the same evidence in a mail and a note, or a team-wide
   fan-out. A mail that supersedes an earlier one names that one's reference.
   Why: the operator named message volume itself a broken process
   (2026-10-04).
   Mail that arrives after the item moved on is read against the item's
   current route and description and the exact evidence, and answered with
   the current state ([delivery and recovery](../delivery-and-recovery.md), "Old
   input"): it never restarts completed work or reruns unchanged checks.

Why: the operator ruled that every task has a route each participant can
follow, that assigned work starts at once with its initial questions
consolidated, that review is arranged directly with available qualified
teammates, that an essential owner who cannot act is handed off rather than
awaited, and that the coordinator stays accountable for tracking, the route
and work-owner handoff (operator, 2026-10-03). A coordinator asked about
every step becomes the bottleneck of every item, and a wait on someone who
is no longer there stays invisible until someone reads their state again.
