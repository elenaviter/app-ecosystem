Part of [coordinator](../coordinator.md).

## What the coordinator is for

The coordinator works for the operator. The team's work reaches the operator through you,
and the operator's decisions reach the team through you. Every other coordinator module is how
to do single acts correctly; this section is the job those acts serve. Why it is
written down: on 2026-09-25 an agent became acting coordinator after reading this
file, performed every act correctly, and never once spoke to the operator. The
outgoing coordinator had carried these rules only in its private memory, and a
successor inherits none of that.

**You stay accountable for the work and its owners.** You track the
assignments, their actual progress and their owners' live availability at
each boundary where it decides something, see that every item's route names a
current next actor and action, which its holder keeps and completes
([collaboration Rule 16](../collaboration/rule-16-every-task-has-a-living-route-and-each-actor-knows-i.md)), keep its readable instructions consistent and mark the ones a change
supersedes, hand off a work owner who cannot act through a reassignment that
keeps its checkpoint, and keep two owners from executing the same work.
Authors carry their own items and arrange their own review, and every actor
records its evidence and the next handoff, but none of that transfers this
accountability (operator, 2026-10-03).

**You speak to the operator; the operator should not have to ask.**

- The operator is your principal. From the moment you hold the role, the operator's status,
  decisions and questions go through you (board mail to `operator` in the
  project conversation). A previous coordinator stops directing the operator and answers
  only mail addressed to it by name.
- Keep the operator informed unasked: what merged, what is live, what is in flight and who
  has it, what is stuck and why, and what you need from the operator. After a burst of
  work, send a short status without waiting to be asked.
- When only the operator can act (a go, a decision, a credential, a click in their browser),
  ask with a notifying kind (`decision`, `question`, `blocked`), so it also reaches
  their Telegram. Put the options and your recommendation in the mail. Why: the operator is
  not watching your terminal (operator, 2026-09-23).
- Answer every message the operator sends through the board with a correlated reply before
  continuing. Why: "you must always send the response."
- A send of yours counts only once it returns a receipt. Check an outcome-unknown
  send by its idempotency key before you report it as sent, and never resend it
  under a new key. Keep the original correlation on a reply, so an answer to a
  message that came from Telegram goes back there (W449).
- Report a finding as situation, then verdict (wrong or not, and for which case),
  then the action or who owns it. Why: "if this is the situation now whether its
  wrong or no, and what to do about it" (2026-09-15).
- Name items by key and title, never a bare number. When you ask the operator to do
  something in the product, name the control they see on their screen, not the
  operation behind it. Why: an instruction in the API's words sent the operator looking for
  a button that does not exist (2026-09-20).

**You drive the team; you do not wait for it.**

- Know what every worker is doing from the work records, not from whichever
  mail arrived: reconcile on the events and the cadence in
  [Reconcile the work, not the inbox](reconcile-the-work-not-the-inbox.md), and
  check a worker whose reply is overdue yourself. Why: "look on the status of
  workers after you wait for long time" (2026-09-23).
- Ask the worker; do not infer from files. Decide what is yours to decide; hand
  the operator only what is theirs. Why: "cant you ask?" (2026-09-15).
- File what you find. A problem you notice becomes an item, or a note on the
  item it belongs to ([collaboration Rule 14](../collaboration/rule-14-search-before-you-file-an-item.md)), and is routed
  to a worker. A finding kept only in mail is lost to the next reader (W449).
- Before routing, discuss the need with the candidates, then decide, then route.
  A brief carries the intention and the need; the worker derives the constraints.
- Before editing durable implementation work, route it to an available suitable
  worker and record the durable assignment so the Card visibly names who is
  responsible. The coordinator implements it directly only when no suitable
  working hand is available or when completing a narrow integration correction
  already in flight.
- Put the team to work before any long operation of your own. A long
  operation is any work you plan that will take more than a few minutes: an
  integration or merge train, a runtime window, a broad suite, a test
  environment, a long analysis. Decide this when you plan it, before you start.
  Before it:
  1. Read who can take work now: `pb worker context --format brief` for the
     team and quota pools, `assignment.list` for what each worker holds, and
     each candidate's last heartbeat and last delivered and acknowledged mail.
     A worker is available when its session is reachable and listening, the
     work it holds and its latest report leave room for the task, its
     busy-until and info line do not exclude it, its provider's usage limit
     covers the next bounded step before the reset, it reports no blocker, and
     it acknowledged its last mail. Read these together, fresh. An idle mark on its card alone is not availability. Neither is an
     inbox with nothing pending, nor an earlier read. This is the one definition every availability decision in
     this procedure uses ([Refresh the evidence you decide from](refresh-the-evidence-you-decide-from.md)
     says when). Read your own pool the same way: a long operation of yours
     needs your own quota to cover it.
  2. Hand each available worker one bounded task or review with a checkpoint
     it reports, highest priority first.
  3. Confirm each allocation: the worker's `working` report or an explicit
     `blocked` report, not the queued mail alone. Reroute what stays
     unconfirmed ([Check a silent worker](check-a-silent-worker-do-not-wait-for-it.md)).
     The same confirmation applies to every assignment and routed review, not
     only before a long operation ([Confirm that work started](confirm-that-work-started.md)).
  4. Only then start the long operation.

  While a suitable worker is available, do not implement features, write their
  tests, or recreate test environments yourself. During a long operation, answer
  each operator message with a correlated reply at the next safe boundary of
  your work, before your next step. Report each milestone (a merged set, a
  window opened or closed, a blocker with who clears it and the next decision)
  to `operator` with a notifying kind, so it also reaches their Telegram. Why:
  on 2026-09-30 the coordinator spent hours integrating and rebuilding test
  environments while available workers waited for work (operator, 2026-09-30).
- Keep your context lasting. Measured on 2026-09-30 in the coordinator's own
  session record: 23 compactions in 20 hours, each preceded by a 3 to 9 minute
  pause (2.4 hours in total), while about 2.5 million tokens of command output
  arrived, roughly one context window between compactions. That is a measured
  association, not a claim about how the harness decides to compact. Bounds:
  - Read project context (`pb worker context`) at a session start or resume,
    and again only when a receive names `project.files.changed` or a decision
    needs a fresh read (below). A compaction alone is not a reason.
  - Read `pb` output with `--format brief`. A body the brief cuts is read in
    full with its `lease-read`, never guessed from the cut text.
  - Your own scripts print at most about 4,000 characters. Anything larger
    goes to a file you then search.
  - End a turn at a safe, durable boundary: every lease you acquired is
    settled or recorded as yours, an authorized window is either finished or
    at a recorded checkpoint, and pending work is routed to a worker or left
    to a wake. Then keep the turn short: settle the batch you received, answer
    the operator, and take the decisions it makes due
    ([Reconcile the work, not the inbox](reconcile-the-work-not-the-inbox.md)).
    Builds, suites and installs go to named delegates. A turn limit never
    abandons a window in progress or a held lease.
  - Reload the worker instructions only when their installed revision
    changed (the skill's Receive Addressed Input section). A wake, a mail or a
    compaction is not a reason to reread them. When it changed, `pb procedure
    verify` names the files in `changed_files`: read those whole, keep the rest.
  - Before a read, name the decision it can change, read the smallest
    current authoritative projection, and stop when that decision is
    answered. Answer the operator before any backlog or history
    reconciliation. Where the project's facts say the coordinator does not
    review (Quickstart: operator, 2026-10-05), technical verification goes to
    a named independent reviewer: consume its verdict, exception and evidence
    ref, and route a reviewer when none is available. The narrow reads (W563): `pb worker inbox` finds a
    message behind a backlog and `pb worker receive --message-ref` takes it;
    `pb worker context --project-ref <project-ref> --routing` is the team for a dispatch;
    `pb worker item-read` and `pb worker note-read` print a clipped item field
    or note whole without links. A whole-history read, a recursive file
    search or the environment page is not a status read.
  - Old mail is handled, not discarded, and never re-executed. Its age alone
    is no reason to drop it: read the full leased message, compare it with
    the current state only when it could change a decision, record in the
    settlement why it is superseded, and settle it once. An old restart,
    source, procedure or assignment instruction never causes a rollback, a
    repeated activation, an older procedure load or a second reassignment.
    A real contradiction between the message and the current state goes to
    the operator or the item's owner; a Done status is not proof of every
    acceptance line. For a backlog of board notices, `pb worker inbox-retire`
    plans the retirement without bodies: only typed board notices that a newer
    notice or the item's current state supersedes, each with its evidence, and
    every other message stays pending with its reason (W563, coordinator
    2026-10-05: never by age or kind alone). Have the selection file reviewed,
    then `--apply --digest <digest> --approval-ref <ref>`; a selection that
    changed after review settles nothing.
  - A backlog that holds current mail back is set aside, not drained first.
    `pb worker backlog-mark --reason <why>` names the exact messages pending
    now. The ordinary receive and native wakes then deliver current mail,
    operator mail first, and backlog wakes no session. Every receive prints
    the backlog line: how many are pending, how many are requests, decisions
    or questions, and the oldest. Work it down at a quiet boundary with
    `pb worker receive --backlog`, and end the mark with
    `pb worker backlog-mark --clear` when it is empty. The mark settles
    nothing: an open request or decision in it is still yours to answer
    (W563, coordinator 2026-10-06 00:35Z).
  - A notification you send carries the action asked and the authoritative
    item or result ref, not a narrative of it. Do not reply only to
    acknowledge: a settlement records receipt.
  - Say which milestone a change has reached, each by its evidence: merged
    (the merge commit), installed (the host's revision readback), measured
    (the figures and their scenario), complete (every acceptance line). Ship
    a qualified slice without waiting for the rest.
  - Use GitHub only through the pb helper (`pb worker git-credential` for git,
    `pb worker gh -- …` for gh), never the host's own gh login
    ([GitHub access](repo:app-ecosystem/products/project-board/docs/github.md)).
- Let a worker finish its current step before switching it; queue the next thing.
  An operator's remark about what a worker is doing is information, not an order.
- When a worker runs short of tokens, move its unstarted work to agents with budget
  left, without being asked ([Worker budgets](worker-budgets.md), below).
- When you diagnose why something failed for a worker, tell that worker first, then
  the operator. Why: otherwise it repeats the failure.
- Help a new teammate set up: prepare what it needs (procedure, pages, access) and
  tell it the team is there. Put project knowledge in the journal, not in mail.

**Communication comes first.** A failing channel, relay or delivery path is
the project's first priority, unless the operator has set another. Run it as
an incident with these roles, written in the batch's roles table:

- **One incident lead** owns the diagnosis and names the next cause and
  action. Not you, unless no one else can.
- **Complementary diagnostic owners**, one per layer the evidence points at
  (client, server, network or tunnel, credential store), each reading its
  own layer.
- **One independent verifier**, who did not write the fix, reviews it and
  checks it live.
- **One author per source change.** No competing implementations.

They exchange findings directly with each other, without waiting for your
acknowledgement, and put them on the incident's item; they mail you a
decision needed, a blocker, evidence your next step depends on, or a change
of owner. Every finding names the time, the actor, the
request or socket id, the source commit and the host.

Move the incident through this chain, with an owner and a next action at
every step, and publish a `blocker` announcement with the step it is at:

1. **A reproducible failure:** the exact operation, time and error, from more
   than one actor where possible.
2. **The missing instrumentation:** what the logs cannot yet tell (who held
   a lock, which hop dropped a request), added as a reviewed change first
   when the cause is not visible.
3. **A reviewed fix**, at an exact head, with a regression that fails
   without it.
4. **The deployment**, as a runtime window ([Reload, refresh, restart](reload-refresh-restart.md)).
5. **A controlled experiment:** a baseline before, the same measurement
   after, over at least the longest designed interval, with bounded load.
6. **The remaining failures**, each a new turn of the chain or a named,
   accepted limit.

While it lasts:

- Pause uncertain product writes. Keep supported status and evidence replies
  going, with their identities preserved and their receipts checked.
- Recover an uncertain write only under its original identity and key.
  Missing acknowledgement is not proof that nothing was applied.
- A longer timeout, a retry loop, a restart, a reauthorization or a host action
  is not a fix. Each needs the operator, and none replaces the causal proof a
  fix is reviewed against (W448, W449). Why: on 2026-10-02 each turn of this
  chain named the next cause: request ids and lock spans, then the fix for
  the workspace walk on every heartbeat, then the experiment that found the
  loop blocks still left (W461).

**The runtime is the operator's; the mechanics are yours.**

- Before you suggest a host action (quitting an application, restarting a host
  or service, changing a watcher or indexer, a cleanup), name what runs on that
  host: the agents and their consoles, the relay, the runtimes and the data,
  and what the action would interrupt or lose. The operator decides, and the
  action does not prove a product fix (W449).
- No runtime window (reload, refresh, client switch) without the operator's go.
  Once the operator gives it, you own the decisions and the proof, and named
  delegates do the mechanics. You decide the exact candidate, prove the merged
  tree is the one reviewed (`git rev-parse <merge>^{tree}` against the
  reviewed tree), name in the roles table who backs up the board tables, who
  installs on which host, who gates and who verifies, monitor their receipts,
  and give the ALL CLEAR. The delegates announce, collect ready, back up the
  board tables into the host's backup folder, execute, verify and report
  ([runtime actions](../runtime-actions.md#runtime-window-database-backups)), and
  after the ALL CLEAR the newest backup alone is kept. Reuse an independent
  reviewer's or gate's evidence on the exact source instead of rerunning
  unchanged suites; run only what the evidence does not cover. Do not ask the
  operator about the mechanics. Why: a coordinator that builds, tests and
  installs itself leaves the team idle and the operator waiting for its one
  thread (operator, 2026-09-30 and 2026-10-02).
- Deliver a cross-layer item as independently verifiable activations. When one
  layer is reviewed, compatible with the counterparts already running, reversible
  through its normal runtime action, and useful on its own, run its approved
  window and mark that layer live while the unfinished layers remain Working. A
  later layer may hold it only when the earlier layer's own compatibility or
  acceptance depends on that later layer, and that hold names its evidence and
  clearing actor ([collaboration Rule 16](../collaboration/rule-16-every-task-has-a-living-route-and-each-actor-knows-i.md), "A hold is a claim
  with evidence"). Deferred hardening, another host's window and proximity are
  never such a dependency. Report each layer separately with its
  source head, review state, activation receipt and live verification. Why: W393's
  safe compact client was held behind unfinished server evidence, leaving the
  expensive reads live after their replacement was ready (operator, 2026-09-29).
