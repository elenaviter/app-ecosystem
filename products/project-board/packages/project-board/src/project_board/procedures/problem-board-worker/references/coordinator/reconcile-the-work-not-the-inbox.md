Part of [coordinator](../coordinator.md).

## Reconcile the work, not the inbox

Read the team's progress from the assignments, never infer it from whichever
mail reached you. A mail says what one sender said. Only the assignments say
who holds what and whether it can move. Why: on 2026-10-02 W295's author
stayed blocked on a missing companion repository binding until the operator
noticed, while the coordinator answered other mail (W466).

**When.** Reconcile on:

- a wake that carries a relevant event: a `working`, `blocked` or `completed`
  report, a review verdict, a refusal, a changed head, a handoff or an operator
  message;
- the 10-minute mark of a dispatch without its STARTED
  ([Confirm that work started](confirm-that-work-started.md));
- about every 30 minutes while a batch is active, and with the roles table and
  the announcement at least every 2 hours.

Not at every tool step, and not as a loop of status polls. Between these
points, end the turn and let the next wake bring the next event.

**What to read.** The plan first: `project.plan.index` with `status` `todo`,
`working` and `review`, narrowed to the batch's items or a small `limit`. It
names each item's current assignee and reviewer, including a review or merge
that waits after the author's assignment completed. Then, for each worker
those rows name, one `assignment.list` with that `worker_name` and `status`
`assigned`, `working`, `blocked` and `accepted`. The list is per worker:
without `worker_name` it returns only your own assignments, so never read the
team from it. For each item, beside the batch's roles table, answer:

1. **Started?** A `working` report at the current ownership version, not mail
   you queued.
2. **Can it move?** The assignment binds every repository the task needs, at
   a base that exists ([Bind every repository](route.md#bind-every-repository-the-work-touches-when-you-assign)).
   A companion repository the task names but the binding lacks is a blocker
   you own, whether or not the worker reported it.
3. **Blocked or waiting?** Its blocker, and who clears it.
4. **What do you owe it?** A decision only you make: a route, a binding, a
   work-owner handoff, a GO, a release, the escalations collaboration Rule 16
   lists, and a review routing or a merge only when no author-arranged
   reviewer or named merger can take it.
5. **A review that sits?** An item in Review whose reviewer has recorded no
   decision, or a hold whose named actor or due time is missing, past, or no
   longer attending ([collaboration Rule 6](../collaboration/rule-6-your-visible-state-says-where-you-are-and-what-you-ar.md), "Unmet
   criteria go back"). Ask that reviewer to decide now, return or hand on.
   When the reviewer is unavailable, route the review to an available one.
   An author nobody has named for a returned item is your route to give
   (W537).

**The team's disk (W547).** With the same read, look at each teammate's
`disk` line in `pb worker context`. It shows the host's free disk, the
workspace size, and the counts from that agent's latest sweep, each with the
time it was observed:

- **Unregistered or orphan folders above 0:** name the agent and ask it
  directly to run `pb worker workspace --sweep`. It then either records each
  finished tree with `pb worker workspace --end --path <tree>` (or registers a
  live one), or moves evidence it keeps into a scratch run.
- **A host below its free-disk threshold** (`disk_alert_free_percent`): ask
  every agent on that host to sweep, starting with the largest workspace.
- **"sweep not reported", or a sweep time older than a day:** ask that agent
  for a fresh `--sweep`. A missing count is unknown, never zero.

Never sweep, end or delete another agent's trees yourself, and never turn on
`--workspace-sweep-auto-apply`: that is the operator's decision for each host.
Why: on 2026-10-04, 32 of 71 folders on one host were unregistered, and nobody
saw it until they were cleaned by hand (2.69 GB, W547).

**What to do.** Turn every finding into one owned next action in the same
turn: take the decision you owe, reassign with the missing binding, route the
review, or name the owner and checkpoint. Write it where the next reader
looks: the instruction in the item or task, the handoff in an item note
([Record each dispatch on the item](route.md#record-each-dispatch-on-the-item)), and
the row in the roles table. A handoff you know is pending (a review waiting
for a head, an install waiting for a GO, a "tell X when Y") is done now or
written as an owned row with a checkpoint, never kept only in your context.
