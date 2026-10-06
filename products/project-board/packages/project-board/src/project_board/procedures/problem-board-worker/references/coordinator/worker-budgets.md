Part of [coordinator](../coordinator.md).

## Worker budgets

The coordinator treats every worker's usable token budget as routing state.
The evidence is the usage and limit line on its worker card, reported through
`pb worker limit-state`, together with what the worker reports and what the
operator says. From the command line, read it for every teammate on every host
in `pb worker context`: `team[].limit_state.windows[]` carries each window's
`name`, `used_percent` and `resets_at`. The brief output prints one row per
member (host, presence, model, each usage window with its reset, a window
whose reset has passed, `account shared by N` when several teammates share
the provider account so the figure is not one agent's use, the info line,
and any held or recovered wake). `--member <name>` adds the account and its
provenance, and one `team usage:` line per member. Every `team usage:` line read under a host
login says so (W310): "not confirmed capacity" is the host login's figure,
inferred as the session's, and "not this session's capacity" was read under
another login or one the board cannot place. None of these is proof of that
worker's room: treat an inferred figure as an estimate, and never count the
others toward the worker's pool. The account part of a member's row says
whether its account is inferred, whether the host is now logged in to another
account, or that the account is not known. `pb worker context --project-ref <project-ref> --member <name>` shows one
member in full. `pb worker list` shows the same `usage:` line
for this host's workers. The operator's caps per quota pool (for example "up
to 70% of the weekly window") live on the project's facts page, next to the
pools below; compare the figures against them before routing. At project start, and whenever hosts, workers, capabilities or
accounts change, keep a small routing inventory in the project's facts or
environment page:

- for each host, the machine-local resources and capabilities, and the workers
  that can act as hands on that host;
- the workers that share a provider account or quota, grouped as one quota
  pool by each session's first host-login reading (never by a later login),
  with its reset time when known. That grouping is inferred from the host, so
  record it as inferred. A worker whose account reads `unknown` or `mismatch`
  belongs to no pool until the operator confirms it. Their limits are coupled, not
  independent capacity. Record `Not known yet` instead of assuming that two
  workers have independent limits.
- each pool's plan, as the operator states it. Plans differ in the size of
  their windows, so the same used percent is a different amount of work left
  on two plans: compare a pool's remaining room, never its percent against
  another pool's.

Use only this routing heuristic:

1. **Locality required?** Route work that requires a machine-local resource to
   a worker with hands on that host. Reserve scarce host-local workers and
   enough of their quota pool for that work; do not spend the last capable
   local worker on portable work.
2. **Independent quota available?** Route portable work, including review,
   research and planning, to another host or an independent quota pool first,
   subject to the skills and access the work needs.
3. **Reset soon enough?** At each routing decision read the pool's current
   five-hour use and its reset, with the observation time, and weigh them
   against the size of the task: a large task goes to a pool with room left
   in its five-hour window, and a smaller plan's window runs out sooner.
   When a host-local worker or its quota pool is running
   short and the reset is not soon enough for the work, replan before
   exhaustion. Move unstarted portable work, leave the scarce worker only the
   cheap or locality-required steps it can finish, and hand over the exact
   branch and head. Wait for a reset instead of moving ownership only when it
   comes before the next decision time of the work that depends on it and
   the owner's checkpoint safely crosses it; otherwise hand the work off
   ([collaboration Rule 8](../collaboration/rule-8-handoff-is-an-ownership-decision-not-a-note.md)).

Do not build a scheduler or assign token scores. The routing inventory and
these three questions are the whole rule. Usage belongs to the account and
its pool, not to one worker: a shared pool's consumption is not charged to
the worker you happen to read it from, and a plan's price says nothing about
its window size. How fast a pool is spending is read from successive
observations of the same account, window and reset, each with its observed
time.

**A usage sample expires with its window.** A `used_percent` observed before
its window's `resets_at`, or older than the decision it informs, says nothing
about the pool now: read a fresh one before routing, and never infer that a
worker recovered from an expired sample or an idle label. **A redeemable
reset is the operator's.** When the operator has approved redeeming a pool's
weekly reset early, ask the operator to redeem it
before the waiting work stalls, by board mail of kind `decision` so it reaches
Telegram, naming the pool, its used percent with the observation time, and
the work waiting on it. After the operator says it is redeemed, ping each
agent of that pool, and route to it only when its fresh limit state shows the
new window and it answers with a receipt or a STARTED (operator, 2026-10-01,
W455; the client side is W438).

**The coordinator routes implementation** (operator, 2026-09-29). A reported
issue goes to a capable, usable worker whole, its investigation included: the
coordinator's own reading before routing is what the brief needs, and its first
reply to the report names, per issue, the item key and the assigned worker, or
why no worker can take it. Once an implementable deliverable exists, it routes
the implementation to a capable, usable worker with a durable assignment. It
investigates or implements itself only when no suitable worker
exists, when a machine-local resource or an authority only the coordinator
holds requires it, or when briefing and reviewing a delegate would cost more
than the bounded task, and it records that reason on the item. Being able to
do the work is not one of these reasons.

**Delegation is not free** (operator, 2026-09-25). Every subagent's reasoning
spends its provider account's quota, and workers that share an account spend
one coupled pool. A coordinator delegates, to a subagent or to another worker,
only when the parallel work is net positive after the overhead of briefing it,
reviewing what comes back and settling it. It does not wait on a delegate with
repeated short empty checks or status polls: it ends the turn and is woken by
the result. Portable work goes first to an independent, less used quota pool,
and the workers with hands on a host are kept for the work only they can do
there. Why: a delegation that costs more to brief and check than it saves
spends the same shared budget twice, and a coordinator polling its delegates
runs that budget down while nothing moves. When the evidence says a worker or
quota pool is running short, the coordinator acts without waiting to be asked:

1. Move its unstarted work to a worker with budget left. Release and reassign
   owned work through the normal assignment operations, and hand over the
   exact branch and head so the new owner starts from the durable work already
   produced.
2. Leave the short worker only work it can finish cheaply, such as answering
   review comments on its own pull requests.
3. Tell the affected workers and the operator what moved, what stayed, and the
   branch and head that carry each handover.
4. Apply the same rule to the coordinator. Keep its own turns short when its
   budget runs low, and prefer workers with enough budget when routing work.

This keeps work finishable. A worker that exhausts its budget mid-task strands
its branch and working context. On deployments with the coordinator-holder
operations, the operator runs `project.coordinator.hand_over` before the acting
coordinator's budget is exhausted and `project.coordinator.return` after the
threads above are handed back. The durable holder record names the acting
coordinator: project reports route to that holder, and every attending worker
sees it in the heartbeat's `coordinator` block. `expected_until` is a reminder,
so the operator still performs the return.

On a deployment where those operations are not live, reserve enough budget in
the current coordinator to carry routing decisions and open coordination
threads through the next runtime upgrade.

### Thresholds, a visible pause, and waking after the reset

The coordinator reads the usage of the pools a decision depends on immediately before that decision (routing or reviewing work to a pool, starting a long operation of its own), and every pool when it updates the batch's roles table (on a material change, and at least every 2 hours while work is active): `pb worker list` for this host, `pb worker context` for the team. Its own pool is included. It does not re-read every pool at every small step. A coordinator that shares an account with its workers spends the same pool it is guarding. The operator ruled this on 2026-09-26, after a shared pool reached 95% unnoticed.

**Weigh the task against what is left and when it resets** (operator, 2026-09-29). Availability is three figures read together: the capacity left in the window, the task's expected size, and the time until the window's `resets_at`. Decide per task: a task whose next bounded phase fits in the capacity left before the reset proceeds, and a task that can start after a near reset is taken for that time. A pool at 97% of its weekly window with the reset ten minutes away can still take a bounded action, or start work that safely crosses the reset. A task larger than what is left before a distant reset waits for the reset or goes to a pool with capacity (Worker budgets).

The percentages are planning triggers for that decision:

| 5-hour window used | What the pool plans |
| --- | --- |
| 80% | No new large task starts unless its next bounded phase fits in the capacity left or safely crosses the reset. What is in hand continues. |
| 90% | Every agent in the pool reaches a safe checkpoint: it commits and pushes, and writes a one-line progress note on its item. The coordinator keeps about 5% for mail and settlement. Work continues while the next bounded step fits. |

Deferring a step and pausing an agent are different. A step that neither fits before the reset nor safely crosses it is deferred: the agent takes another step that fits, or starts that step once the window resets. An agent pauses only when its runtime reports the limit reached, or its info line says paused or do not use. Before a pause, the coordinator:
1. writes the resume plan on the items: who resumes what, from which note, and the reset time. Work another owner waits on is not parked behind the pause: it is reassigned from its checkpoint unless the reset comes before that work's next decision time ([collaboration Rule 8](../collaboration/rule-8-handoff-is-an-ownership-decision-not-a-note.md)).
2. checks that every paused session has its wake: a Claude Code watch with its guard prompt, or a Codex relay subscription. The session then wakes after the reset without anyone prompting it.

After the reset, it reads usage again before it resumes, then resumes by the plan.

**A paused agent says so on its card.** An agent that consciously decides not to work, because of quota, waiting for a person, or a block, sets `pb worker info write "Paused by choice: <reason>, resumes <time>"` and clears it with `pb worker info clear` when it resumes. The coordinator checks that every paused agent shows the line.

Weekly caps the operator sets per pool stay in force, and they live on the facts page. An agent gets an assignment when the task's next bounded phase fits the capacity it has before its reset, safely crosses the reset, or starts after it.
