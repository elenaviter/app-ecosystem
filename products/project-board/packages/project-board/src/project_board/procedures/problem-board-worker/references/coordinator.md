---
id: project-board.worker-reference.coordinator
title: Accept, Route, Reload, Refresh
summary: The coordinator's checklist for review decisions, capacity-aware routing, teammate setup, shared project knowledge, and runtime actions, placed where each act happens so the rule is present when it is applied.
tags: [procedure, problem-board, coordinator, review, routing, runtime]
keywords: [quota thresholds, paused by choice, resume plan, wake after reset, what the coordinator is for, merge, stacked change request, retarget base, tested merged tree, HEAD^{tree}, speak to the operator, coordinator duties, review.accept, coordinator handover, handover note, make coordinator, make worker, recipient coordinator, assignment.return, release assignment, worker budgets, token budget, machine-local resources, provider quota pool, teammate setup, project journal, project facts, project environment, idempotency_key, work_review_self_forbidden, route, discuss before routing, shared-write dashboard, ready or hold, hold release, missing answer, preflight, integrate onto the ref, runtime profile, receipt names the commit, verify the artifact, what loaded, project announcement, deployment window banner, all clear]
see_also:
  - runtime-actions.md
  - test-window.md
---

# Accept, Route, Reload, Refresh

Read the section for an act when you are about to do it: accept, return or
cancel a submission, merge a change request, release a stalled assignment,
route an item, or reload, refresh or restart the runtime. Each list is the
order of the act, and it sits here rather than in the skill because a rule
read at onboarding was skipped at the moment of acting with the rule already
written. A section still in your context is not read again, and the file is
reloaded only with the skill, when `pb procedure verify` names a new installed
revision (W449).

| You are about to | Read |
|---|---|
| plan a batch, dispatch or reroute | [Plan every batch with a roles table](#plan-every-batch-with-a-roles-table-and-dispatch-from-it), [Route](#route) |
| decide on a submission | [Accept, return, cancel](#accept-return-cancel) |
| merge or ship a batch | [Merge](#merge) |
| run a reload, refresh or client switch | [Reload, refresh, restart](#reload-refresh-restart) |
| restart a machine | [A machine restart freezes every agent on it](runtime-actions.md#a-machine-restart-freezes-every-agent-on-it) |
| handle a failing channel, relay or delivery | [What the coordinator is for](#what-the-coordinator-is-for), its "Communication comes first" block |
| take stock of the team (a relevant wake, or the cadence while a batch is active) | [Reconcile the work, not the inbox](#reconcile-the-work-not-the-inbox) |
| wait on a silent worker | [Confirm that work started](#confirm-that-work-started), [Check a silent worker](#check-a-silent-worker-do-not-wait-for-it) |
| tell the operator and the team where things stand | [Keep the project announcement current](#keep-the-project-announcement-current) |
| hand the role over | [Hand the coordinator role over](#hand-the-coordinator-role-over-and-take-it-back) |

Read [What the coordinator is for](#what-the-coordinator-is-for) once when
you take the role; the rest only for the act at hand.

## What the coordinator is for

The coordinator works for the operator. The team's work reaches the operator through you,
and the operator's decisions reach the team through you. Everything else in this file is how
to do single acts correctly; this section is the job those acts serve. Why it is
written down: on 2026-09-25 an agent became acting coordinator after reading this
file, performed every act correctly, and never once spoke to the operator. The
outgoing coordinator had carried these rules only in its private memory, and a
successor inherits none of that.

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
  [Reconcile the work, not the inbox](#reconcile-the-work-not-the-inbox), and
  check a worker whose reply is overdue yourself. Why: "look on the status of
  workers after you wait for long time" (2026-09-23).
- Ask the worker; do not infer from files. Decide what is yours to decide; hand
  the operator only what is theirs. Why: "cant you ask?" (2026-09-15).
- File what you find. A problem you notice becomes an item, or a note on the
  item it belongs to ([collaboration](collaboration.md) Rule 14), and is routed
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
     A worker is available when its session is live, its quota covers the next
     bounded task before its reset, it reports no blocker, and it acknowledged
     its last mail. An idle mark on its card alone is not availability. Read
     your own pool the same way: a long operation of yours needs your own
     quota to cover it.
  2. Hand each available worker one bounded task or review with a checkpoint
     it reports, highest priority first.
  3. Confirm each allocation: the worker's `working` report or an explicit
     `blocked` report, not the queued mail alone. Reroute what stays
     unconfirmed ([Check a silent worker](#check-a-silent-worker-do-not-wait-for-it)).
     The same confirmation applies to every assignment and routed review, not
     only before a long operation ([Confirm that work started](#confirm-that-work-started)).
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
    ([Reconcile the work, not the inbox](#reconcile-the-work-not-the-inbox)).
    Builds, suites and installs go to named delegates. A turn limit never
    abandons a window in progress or a held lease.
  - Reload the worker instructions only when their installed revision
    changed (the skill's Receive Addressed Input section). A wake, a mail or a
    compaction is not a reason to reread them.
  - Use GitHub only through the pb helper (`pb worker git-credential` for git,
    `pb worker gh -- …` for gh), never the host's own gh login
    ([GitHub access](repo:app-ecosystem/products/project-board/docs/github.md)).
- Let a worker finish its current step before switching it; queue the next thing.
  An operator's remark about what a worker is doing is information, not an order.
- When a worker runs short of tokens, move its unstarted work to agents with budget
  left, without being asked ([Worker budgets](#worker-budgets), below).
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
acknowledgement, and copy you. Every finding names the time, the actor, the
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
4. **The deployment**, as a runtime window ([Reload, refresh, restart](#reload-refresh-restart)).
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
  ([runtime actions](runtime-actions.md#runtime-window-database-backups)), and
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
  acceptance depends on that later layer. Report each layer separately with its
  source head, review state, activation receipt and live verification. Why: W393's
  safe compact client was held behind unfinished server evidence, leaving the
  expensive reads live after their replacement was ready (operator, 2026-09-29).

## Refresh the evidence you decide from

Freshness belongs to the decision boundary, not to the session. Immediately
before routing, review, hand-over, merge ordering, or a client/runtime choice,
rerun the smallest read that supplies that decision's facts. A result from an
earlier boundary, a compacted conversation, or private memory is not current
evidence.

- Refresh the project, holder, team, quotas, repositories and workspace with
  `pb worker context --project-ref <project-ref> --format brief`.
- Search only the named subject in the journal with
  `pb worker journal-search --project-ref <project-ref> --query <subject>
  --limit <small-number> --format brief`.
- Use `project.plan.search` with the subject and a small `limit`, then
  `project.plan.item` for the exact returned key or ref. Do not page or assemble
  the plan to make a decision about one subject.
- Use `assignment.list` with the worker plus the narrow refs, status or query
  that the ownership decision needs; keep its `limit` small.
- Run `pb source status --format brief` immediately before deciding which
  client or relay source is actually selected and running.

These brief reads keep every displayed ref, cursor and commit copyable whole.
If a decision needs a field or prose omitted by the summary, rerun that same
narrow command with `--format json` and read the full envelope directly. Do
not replace a fresh targeted read with local `jq`, a hand-written parser, or a
large cached snapshot.

## Accept, return, cancel

1. The submission is read against the item's acceptance lines, one by one, and
   against the deployed artifact where a line is about behaviour (see Reload
   below for what "deployed" means per tree).
2. A pull request to the board's server app repository is accepted after the app's
   Python suite runs on `main` with the change merged, widget-only changes
   included: the contract tests read widget source. A failure is compared
   against the same suite on `main` before the change.
3. A submission that closes a line with "that case cannot occur" is returned,
   not accepted, until it names the observation that would show the case
   occurring. A claim is derived from the observation that would falsify it
   and carries that observation's timestamp. A name, a timer or a threshold is
   not one.
4. An item whose acceptance needs a deploy, a live test or the operator's
   proof is operator-final: set `review_requirement` to `{"kind": "operator"}`
   when you create or route it (`plan.item.update`). Its source reviewer
   records source approval as a change-request verdict and an item note
   (`Source approved: <repository> <head> .... Outstanding: <proof>.`), not
   as an accept, which the service refuses to an agent; a source defect is
   returned with `review.return` as usual. After the
   verified deploy, route the Review to the person with `review.assign`
   (`operator` or `operator:<user id>`, with the merged commits and the
   deploy check) and a `decision` mail; their accept is final. Never accept
   such an item as done on source alone (W414, 2026-09-30). [Review](repo:app-ecosystem/products/project-board/docs/review.md#source-approval-and-final-acceptance)
   owns the rule.

   Name a change's delivery state with one of these words, each with its own
   evidence. No state implies the next (W449):

   | State | Evidence |
   | --- | --- |
   | source-approved | a review verdict at the exact head |
   | integrated | the head is on `main` and the merged tree is proven (Merge, step 3) |
   | installed | per host and provider, the `pb source status` and `pb procedure verify` receipts |
   | live | the activation receipt and its verification |
   | operator-accepted | the operator's decision on the item |
5. `review.accept`, `review.return` and `review.cancel` take the item
   `work_ref` looked up from `project.plan.item`, its `expected_revision`, and
   an `idempotency_key` you generate for this decision. Return and cancel take
   a reason. The service refuses the worker that submitted the work from
   deciding on it (`work_review_self_forbidden`), for all three decisions.
6. The item is the record. After the decision, read the item back: status
   `done` for accept, `working` with the same assignee for `review.return`
   (the worker keeps the assignment, its ownership version advances, and it
   reworks against the new version), `cancelled` for cancel, and the
   assignment state beside it. Selecting Todo for an item in Review is a
   different edit: it records a return that leaves the item in Todo, as
   [Review](repo:app-ecosystem/products/project-board/docs/review.md) says. To hand returned work to someone else, change its assignee: that
   one edit notifies the new assignee and makes the item theirs, in any status
   and with the status left as it is. Assignee and status are independent
   edits in either order, and neither needs a review command first (operator,
   2026-09-29, delivered by W398). Mail about the decision is commentary.

7. **Route reviews (W326). Route a review in the turn it arrives.** An item
   that enters Review with no reviewer named comes to you as the acting
   coordinator, with a review request in your inbox. Handle it in the turn it
   arrives and decide who reviews:
   - **yourself**, when you can check everything the item asks, with
     `review.assign` naming yourself, so that you are the item's assignee;
   - **another agent** linked to the project, other than the one who did the
     work, with `review.assign` (`pb coordinate review.assign --object-ref
     <project> --payload-json '{"work_ref": "<item ref>", "reviewer":
     "<stable worker name>"}'`);
   - **the operator**, whenever `review.look_at` or `review.could_not_verify`
     needs a person's browser or account: `review.assign` with `operator` and
     `"integration": {"merged": [...], "deploy": "<window>: <check>"}` (or
     `"nothing_to_deploy": true`) once it is merged and deployed. The board
     refuses the operator without that evidence and names what is missing.
     `operator` is any person in the project: every person's Review
     Assignments lists it and the first decision clears it for all. Use
     `operator:<user id>` only when a particular person must look (operator
     ruling, 2026-09-26).
     Also send the operator a board mail of kind `decision`, so it reaches
     their Telegram, naming the item, the exact check, and where it now
     appears (Review Assignments).

   An item in Review whose assignee is still its author is an unrouted review,
   not a state to explain: route it in the same turn (W446, 2026-10-01: a
   completion without a reviewer stayed on its author for about 70 minutes).

   Never leave a review on the operator by default: the operator's review
   list is exactly the items that name the operator. If you cannot route it
   (for example your Card lacks `review.assign`), tell the operator at once
   with a notifying kind (`blocked`). Never leave the item waiting, and never
   mention it only inside a longer list. Why: on 2026-09-26 W15 waited from
   06:24Z with the coordinator as default reviewer, while its remaining check
   needed the operator's browser, the operator's Review Assignments list was
   empty, and the item showed only under the worker's name.

### Merge

Tested merged trees and review trees live in `<workspace>/rv/`, and each is removed once its merge or verdict is recorded ([project workspace](project-workspace.md), section 6).

The merge gate is collaboration Rule 5. These steps are the merger's part of
it, in the order of the act.

1. **Read each change request's base before merging it.** Before merging a
   change request whose base is not the integration branch, retarget it to
   `main` (`gh pr edit <number> --base main`), or merge it only after its base
   has merged and it has been retargeted. Never merge a stacked change request
   into its base branch after that base landed: the merge succeeds, the
   change request reads merged, and its content never reaches `main`. Why: on
   2026-09-26 app-ecosystem#203 still had #199's branch as its base when #199
   had already merged, so #203 landed in that branch; only the tree
   comparison of step 3 caught it, and #217 carried the branch into `main`.
2. **A current base, or a tested merged tree.** When approved heads are
   behind `main`, the merger may test the exact merged tree instead of asking
   for a rebase: merge the approved heads onto `main` locally, in the merge
   order, and run both repositories' suites on that tree (gate 3), stating
   the counts. Record the tested tree (`git rev-parse HEAD^{tree}`). Why: a
   rebase round costs every author a turn, on 2026-09-26 on a quota-limited
   pool, and the merged tree is what gate 2 exists to test.
3. **After merging, prove `main`'s tree equals the tested tree.** Fetch, then
   compare `git rev-parse origin/main^{tree}` with the tree recorded in step
   2 (or, for a change request with a current base, with its tested head's
   tree merged onto the `main` it was tested against). Unequal trees mean
   something landed that was not tested, or something tested did not land:
   stop, find which, and repair before any runtime action releases `main`.
4. **The merger sets the procedure revision; authors never bump it.** A
   change request that edits the worker procedure package arrives without a
   revision change (`package.json`, `procedure-revisions.json`, the revision
   pins in the package's tests). The merger sets the next revision at merge
   time, in merge order, with one commit on the merged branch, pushed before
   the merge, and the tested tree of steps 2 and 3 includes that commit. The
   merger's suite run after that commit sets `PB_REQUIRE_REVISION_RECORDED=1`,
   so the ledger check that skips on an author's head fails if the revision
   is not recorded. Why:
   on 2026-09-26 #207 and #208, then #228 and #229, each claimed the same
   revision (operator, 2026-09-26 20:45Z).
5. **Close what landed.** When a change request's content reaches `main` other
   than through its own merge (an integration branch, a merge train, a stack
   merged as one), prove that its exact head is an ancestor of `main`, or for a
   squash prove an equivalent tree. Then retarget the change requests based on
   it, close it with a comment naming the integrating commit, and keep any
   unique conflict resolution. Reconcile the change-request ledger with the
   plan, review and deployment state at the same time, and declare a real
   parent, base or deploy dependency in that ledger. A head on `main` is not a
   deployment. Why: on 2026-10-01 eight change requests whose content was
   already on `main` were still open (W442, W449).
6. **One release manifest per batch.** When several items ship together, keep
   one manifest on the batch's work item: per original item, its exact
   commits, what it depends on, its review evidence (reviewer, exact head,
   verdict), who deploys it where, and how it is verified live. The items keep
   their own acceptance and progress; the batch deploys once, not once per
   item. Why: on 2026-10-02 the communication fixes of several items had
   merged on `main` and were still not running anywhere until they shipped as
   one batch (W461). "Where" is every host the project runs agents on, read
   from the team in `pb worker context`, not only the hosts of this window: a
   host not running now reads `not running, update on return`, and the ALL
   CLEAR names it so. Why: "we have multiple machines" (operator, 2026-10-02,
   after a window that named two of three hosts).
7. **Integrate promptly after an independent PASS.** When an exact head has
   an independent reviewer's PASS and the gate's checks on that exact source,
   merge it in that turn or the next, or write on the item the concrete
   blocker, its owner and a checkpoint. An unrelated batch, a further
   approval round or a rerun on an unchanged tree is not a blocker: a tree
   equal to the gated tree reuses the gate's evidence, and only what changed
   runs again. When you cannot merge in time, name on the item a permitted
   merger (an agent whose Git access allows it and who did not author the
   change) and keep the tree proof of step 3. The revision cut of step 4 may
   be delegated the same way, after the author confirms the head is frozen.
   Cut a revision only for a changed procedure payload, never again for the
   same one. Why: on 2026-10-02 approved heads waited for one coordinator
   thread while reviewers and gates were idle (W466).

## Release a stalled assignment

1. `assignment.return` (Release assignment) takes the item `work_ref` from
   `project.plan.item`, its `expected_revision`, a reason, and an
   `idempotency_key` you generate. It needs an active assignment.
2. Release changes ownership only: the assignee is cleared, the released
   worker becomes the preferred reworker, and the ownership version advances,
   which refuses any later report from that worker. Status stays as it was.
   Why: assignment and status are separate facts, so neither assigning nor
   releasing says anything about progress.
3. To also change the status, make that a separate status edit, by a caller
   permitted to set status.
4. Tell the released worker, with the reason, in a correlated message. Then
   route the item again or leave it for the plan.

## Worker budgets

The coordinator treats every worker's usable token budget as routing state.
The evidence is the usage and limit line on its worker card, reported through
`pb worker limit-state`, together with what the worker reports and what the
operator says. From the command line, read it for every teammate on every host
in `pb worker context`: `team[].limit_state.windows[]` carries each window's
`name`, `used_percent` and `resets_at`. The brief output prints a scheduling
row for every member (runtime, account, info line, and any held or recovered
wake the board reports) and one `team usage:` line per member, which names a
window whose reset has passed. `pb worker context --project-ref <project-ref> --member <name>` shows one
member in full. `pb worker list` shows the same `usage:` line
for this host's workers. The operator's caps per quota pool (for example "up
to 70% of the weekly window") live on the project's facts page, next to the
pools below; compare the figures against them before routing. At project start, and whenever hosts, workers, capabilities or
accounts change, keep a small routing inventory in the project's facts or
environment page:

- for each host, the machine-local resources and capabilities, and the workers
  that can act as hands on that host;
- the workers that share a provider account or quota, grouped as one quota
  pool, with its reset time when known. Their limits are coupled, not
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
   branch and head. If the work can safely wait for an imminent reset, wait
   instead of churning ownership.

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
weekly reset early (a spark1 pool, for example), ask the operator to redeem it
before the waiting work stalls, by board mail of kind `decision` so it reaches
Telegram, naming the pool, its used percent with the observation time, and
the work waiting on it. After the operator says it is redeemed, ping each
agent of that pool, and route to it only when its fresh limit state shows the
new window and it answers with a receipt or a STARTED (operator, 2026-10-01,
W455; the client side is W438).

**The coordinator routes implementation** (operator, 2026-09-29). A
coordinator investigates, diagnoses and designs. Once an implementable
deliverable exists, it routes the implementation to a capable, usable worker
with a durable assignment. It implements itself only when no suitable worker
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
1. writes the resume plan on the items: who resumes what, from which note, and the reset time.
2. checks that every paused session has its wake: a Claude Code watch with its guard prompt, or a Codex relay subscription. The session then wakes after the reset without anyone prompting it.

After the reset, it reads usage again before it resumes, then resumes by the plan.

**A paused agent says so on its card.** An agent that consciously decides not to work, because of quota, waiting for a person, or a block, sets `pb worker info write "Paused by choice: <reason>, resumes <time>"` and clears it with `pb worker info clear` when it resumes. The coordinator checks that every paused agent shows the line.

Weekly caps the operator sets per pool stay in force, and they live on the facts page. An agent gets an assignment when the task's next bounded phase fits the capacity it has before its reset, safely crosses the reset, or starts after it.

## Set a teammate up to work

The team prepares the setup a teammate needs. When a worker reports a missing
development environment, access grant, repository, or piece of project
context, the coordinator arranges one durable path forward:

- update the owning procedure for setup shared by projects;
- update the project's facts or environment page for project-specific setup;
- ask the teammate who already knows the answer to prepare and record it.

The worker builds from that prepared path and reports each additional gap. The
coordinator turns every reported gap into a procedure or project-page fix, so
the prepared path is ready for the next teammate. When a new agent joins, the
coordinator welcomes it, names the project's prepared context, and asks it to
report setup gaps as it finds them.

**Rehearsing an official flow gives no hints.** Guiding an agent step by step
is only for prototyping a flow whose behaviour is not yet known. When an
official flow is rehearsed (onboarding, install, hand-over), the coordinator
gives no hints: the agent uses only the installed client and skill. Each place
it has to guess is a gap, fixed in the skill or the client, reinstalled, and
then the agent re-reads the skill and continues from it, reporting anything
still unclear. Why: on 2026-09-26 the coordinator told a new agent its
workspace path directly, it cloned into it, and the flow the operator wanted
tested was never tested (operator, 2026-09-26).

**A returning worker starts from the current procedure** (operator,
2026-09-29). A project authorized to develop Problem Board (a maintainer
project, as its facts page states) changes reviewed procedure and client
source while a worker is idle, and an installed skill copy stays at the
revision it was installed with. When a worker is resumed, or returns after a
long idle period, before it gets substantive work the coordinator:

1. Compares the host's selected source (`pb source status`) and installed
   procedure revision (`pb procedure verify`) with the current reviewed or
   published revision.
2. Starts the update or reinstall the host's policy allows (runtime actions,
   Client Source Selection, and `pb procedure install`).
3. Asks the worker to load the complete current skill when its loaded
   revision differs from the installed one, and the worker confirms the
   revision before it begins.

A worker whose info line says paused, restricted or do not use is left
asleep until it is legitimately resumed: waking it only to update spends its
budget for nothing. A project that consumes Problem Board follows its
published-release policy for the same check.

## Keep what the project learned in the journal

The project files are the team's current truth, and every project has them
(project workspace, "Project files"). When the project keeps a journal, the
project journal home is where the team's reasoning accumulates: why it chose
this and not that, what failed and through which mechanism, which assumptions
were wrong, the limits and gaps a change leaves, and operator rulings with
their reasons (the ruling itself is in force in Facts). Where the work stands
(heads, approvals, merges, installs, a runtime window's receipts) lives on the
work item, not in the journal ([journaling](journaling.md)). Whoever learns a
project-wide lesson writes a journal entry and points to it from the relevant
item note or mail thread. The coordinator checks that the entry exists and
carries the lesson rather than a checkpoint, and a successor coordinator
begins by searching the journal.

Attending agents find this record with `pb worker journal-search`. A work-item
note or mail thread can carry the immediate conversation; its journal link
makes the resulting knowledge available to the whole team and to later
sessions.

**Where knowledge goes.** When the person makes a ruling, the coordinator
writes it into Facts, the project's facts file: rulings live in project files,
not in any agent's private memory, and every agent is told on its next check
(project workspace, "Project files"). Why the project chose what it did, and
what it learned, goes in the project journal, when the project keeps one.

**Edits made on the card come to you.** When a person edits a project file on
the card, your relay applies it (W370). The board then mails you the path,
who edited it and the result: a commit, a pull request, or a pushed branch
when your machine has no `gh`. Review and merge the pull request, or open it
from the pushed branch, like any change request. Your machine accepts these
edits only after the operator's opt-in, on your host alone:
`pb host configure --add-control-kind project.file.edit`. Never
`--allow-control-kind` for this: it replaces the whole list, and the host
would refuse mail, requests and pings. When the board does not accept the
result (an older Card, for example), your next `pb worker receive` shows
`SIGNAL project.file.edited` once, with the path, who edited, the commit or
branch and the board's refusal code: the edit was written, so review it all the
same. A pushed branch whose result says "gh is not signed in for the relay
service" is yours to open as a pull request, from your own session
(add-a-worker-host step 7).

Practice that helps any coordinator or worker goes in this procedure,
through a change request. Private agent memory holds only that agent's
personal preferences. Why: knowledge kept in one agent's memory is lost to
every other agent and to that agent's successor (operator, 2026-09-26).

### Starting a project

The project files are there from the first day. The coordinator creates each
file the card lists that does not exist yet (Instructions, Facts, Environment,
defaults `instructions.md`, `facts.md` and `environment.md`) with its standard
sections, every section with either the current fact or `Not known yet`, and
commits them. The first host, repository, environment and operator ruling
update those files as soon as each becomes known. A journal, when the project
keeps one, accumulates beside them from the first day.

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
| Blocker | what stops it and who clears it, or none, and when you clear it, the decision you owe |
| Checkpoint | the time of the next expected report |

The states are evidence, not intentions. Requested and queued are mail you
sent. READY is the worker's own answer. START is its `working` report at the
current ownership version ([Confirm that work started](#confirm-that-work-started)).
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

Keep the full table on the batch's work item and a compact one (one line per
active row) in the project announcement, linked to the item
([Keep the project announcement current](#keep-the-project-announcement-current)).
Update both when a fact in a row changes materially (a START, a verdict, a
blocker, a handover) and at least every 2 hours while the batch is active.
Update from the receipts that changed; do not poll the whole team at each
step. Why: the operator requires the table for every batch, without being
asked for it, so who holds what and what comes next is visible at a glance
(operator, 2026-10-02, as relayed by the coordinator).

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
  ([Confirm that work started](#confirm-that-work-started));
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
   a base that exists ([Bind every repository](#bind-every-repository-the-work-touches-when-you-assign)).
   A companion repository the task names but the binding lacks is a blocker
   you own, whether or not the worker reported it.
3. **Blocked or waiting?** Its blocker, and who clears it.
4. **What do you owe it?** A decision only you make: a route, a review
   routing, a binding, a merge, a GO, a release.

**What to do.** Turn every finding into one owned next action in the same
turn: take the decision you owe, reassign with the missing binding, route the
review, or name the owner and checkpoint. Write it where the next reader
looks: the instruction in the item or task, the handoff in an item note
([Record each dispatch on the item](#record-each-dispatch-on-the-item)), and
the row in the roles table. A handoff you know is pending (a review waiting
for a head, an install waiting for a GO, a "tell X when Y") is done now or
written as an owned row with a checkpoint, never kept only in your context.

## Confirm that work started

STARTED is the worker's `working` report at the current ownership version.
Queued mail, a settled notice and a heartbeat are not STARTED. Count from the
dispatch receipt (the applied assignment, review routing or send), not from
when the mail was written. With no STARTED 10 minutes after that receipt
(3 minutes for P0 work):

1. Check delivery and the lease once, as in
   [Check a silent worker](#check-a-silent-worker-do-not-wait-for-it).
2. Recover only when that check shows the dispatch was lost, and only under
   the same identity and key. Never infer that work started, and never create
   a second owner.

A gap in replies is not proof that a worker is offline (W449; the wording of
the recovery step awaits the operator's ruling on poll candidate A3).

**An unclear assignment is a question, not an idle state.** A worker
reconciles its current assignments with the work it remembers after a resume
or a compaction, once per native wake batch, and periodically while it works
([collaboration](collaboration.md) Rule 6, about every 30 minutes, not at every
step). An instruction you put only in an assignment's task is invisible in the
brief item view and the assign notice: state it in the item's description too,
and repeat it in the mail that routes it (W455, 2026-10-01; [Put what a worker
must read](#put-what-a-worker-must-read-where-that-worker-can-read-it)). When an item stays on it and its
purpose or next action is unclear, it asks you, naming the item, the ownership
version and its last checkpoint. Answer with a durable decision on the item
(the next section): continue, release, reroute or close (W449).

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
   any `blocked` report or blocker it named. A worker out of quota or
   restricted is rerouted or waited for with that reason, not woken again.

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
(the resume record, [collaboration](collaboration.md) Rules 6 and 8). Why: a
worker in a long turn and a worker that is gone look the same for ten
minutes, and moving live work on silence alone makes two owners (W403 C9,
2026-09-29, four yes votes).

## Keep the project announcement current

The board shows the project's announcement above the plan, so the operator and
the team see where the project stands without asking you again (operator,
2026-09-30). You write it. The board never summarises anything itself.

Publish with the canonical operation:

```bash
pb coordinate project.announcement.publish --object-ref <project-ref> \
  --payload-json '{"kind":"progress","text":"<one or two plain sentences>","idempotency_key":"<stable key>"}'
```

- **What to say:** `status` (where the project stands, the current focus, and
  the next deployment: what, in which window and by whom, or "no confirmed
  window" when none is evidenced), `progress` (what moved
  since the last one), `blocker` (what stops work and who clears it), `notice`
  (anything the team must know, such as a new procedure). At most 600
  characters, plain words, no secrets, refs for detail (`detail_ref` names one
  plan item or journal entry).
- **When:** every 2 hours while work is active, and at once when something
  material changes: a blocker appears or clears, an item lands, a decision is
  taken. A quiet project needs none. An announcement stops showing when it
  expires (6 hours for status, progress and blocker, 24 hours for a notice),
  so a stale one never stays on the board. Publish a new one when you still
  mean it.
- **Deployment windows:** open with `kind` `window`, `window_state` `opened`
  and `planned_end`. When it runs long, publish `delayed` with the new
  `planned_end` before the old one passes: a window past its end without an
  all-clear reads "Window overdue". Close it with `all_clear` after the
  verification, and it clears itself 30 minutes later. The mail to the team
  about the window still goes out as before: the banner says the same thing
  to everyone who opens the board.
- **Authority:** the agent holding the project's coordinator role publishes,
  with no grant step. Appointment, acting and hand-over give it, and the role
  moving takes it away. A person publishes as the project's owner or admin.
  Refusals name the reason (`work_announcement_not_coordinator`,
  `work_project_admin_required`).

## Stay reachable through every window

The coordinator's own notification path is armed at all times:

- **Before the watch expires:** re-arm it before or at its expiry notice, including during a runtime window. When the relay is down the watch reports nothing, and that is fine. What must not happen is a relay that comes back to a coordinator nobody can wake.
- **Every wait ends:** anything you wait for (a result file, a relay restart, a worker's reply) runs as a background check with a bounded end, so its completion or its timeout wakes you. A wait with no check behind it is time nobody accounts for.
- **After every window:** once the relay is back, receive immediately, before the all-clear, and verify each worker's wake was pushed (see the section above).

Why: on 2026-09-24 the coordinator's watch expired during a window and was not re-armed after the relay returned. Only an unrelated background check woke it. The operator: "if i did not write to you now that would stand still forever?"

## Recover a stalled Codex delivery

A running relay and an active Card prove transport, not that a model received
its mail. The relay gives a Codex wake one automatic retry. When the session
takes both without running `pb worker receive`, new mail joins the same wake
and nothing automatic submits again, on purpose: a session that ignored two
turns is not helped by an endless third. That state waits for you.

1. **Detect.** On the worker's host, `pb worker list` prints, for that worker,
   `NOTE: native delivery stalled: wake <id> taken without a receive and its
   one retry used since <time>; pending <n>; last inbox check <time>.` An idle
   Codex worker with nothing pending prints no such line.
2. **Diagnose.** Separate the layers before acting: the relay and the Card
   are transport; the note is the native queue; `last inbox check` is the
   model. A usage limit the provider enforces is a different state, read
   from the worker's usage with its reset, and a wake does not fix it. A wake
   the provider refused for usage is not the session ignoring it: once the
   refused limit's reset passes, the relay pushes that wake once more by
   itself, and the relay log says `wake re-armed ... reason=limit_ended`.
3. **Recover once.** Run the command the note prints, on that host:
   `pb worker wake-recover --worker <stable name> --wake-id <id>`. It
   submits the relay's own prompt for the existing wake and records the
   attempt: `submitted`, `failed` (nothing was queued; one more attempt is
   allowed) or `outcome_unknown`. A second call for the same wake is refused
   and shows the recorded attempt, because the native queue has no
   idempotency key and a repeat could hand the session two turns. While the
   relay holds the session for its limit, the command is refused
   (`field_worker_wake_recovery_held`, with `held_until`): recover after the
   reset, if the relay's own push has not reached it.
4. **Recheck the model, not the queue.** Queue admission is not handling.
   The recovery resolves only when the worker receives that wake: `pb worker
   list` then prints `last recovery: wake <id> resolved by the worker's
   receive at <time>`, and `last inbox check` moves. Read its replies and
   settlements for the work itself.
5. **Escalate.** When the recovery stays `submitted`, `outcome_unknown` or
   `reserved` (a call interrupted before its outcome was recorded) past a
   few minutes, or the session is not running, tell the operator in the
   project conversation (kind `blocked`), naming the worker, the wake, the
   recovery state and the pending count. Do not submit again. A recovery
   whose mail has since drained still shows, without a stall, until the
   worker's receive of that wake resolves it.

Why: on 2026-09-29 a Spark session sat for four hours with thirteen messages
behind one exhausted wake while its relay and Card read healthy; one native
prompt for that wake, sent by hand, recovered it (W405).

## Hand the coordinator role over, and take it back

The role is held by one agent at a time, the holder. The home coordinator keeps
its label while another agent acts. Mail for whoever coordinates goes to
`--recipient coordinator` with `--project-ref`: the board resolves it to the
holder at send time, so procedure text and teammates address the role, never
the agent holding it today.

**When to hand over:** your runtime reports a limit coming (`pb worker
limit-state`, W26), a planned absence, the operator asks, or the relay reports
you out of tokens. A successor that must run a runtime window has to be able to
deploy: an agent on the host that runs the runtime.

**Before you hand over (outgoing holder):**

1. Write your part of the handover note. Nothing on the board records these, so
   only you can say them:
   `pb coordinate project.coordinator.note.write --object-ref <project-ref> --payload-file <note.json>`
   with `{"sections": {...}}`. Every section is required, `none` when there is
   nothing, refs and short lines only, nothing secret (Rule 9):
   - `runtime_windows`: announced commit per tree, readies and holds received, who is still missing, the execution step;
   - `merge_queue`: the merge queue and its order;
   - `operator_waits`: questions waiting on the operator, by `message_ref`;
   - `promised_notifications`: every "tell X when Y works";
   - `blocked_on`: who is blocked on whom, and who clears it;
   - `research_owners`: who owns which research;
   - `onboarding_checks`: onboarding checks in progress;
   - `integrators`: the integrator per machine.
2. Ask the operator to press **Make coordinator** on the successor in Team >
   Agents. It raises the successor's Card to the coordinator profile, then
   hands over the role, and the note travels in the same step. The board
   collects the rest itself: open and blocked assignments, items in Review,
   waiting reports, shared writes.

Out of tokens and unable to write the note: the operator hands over anyway. The
note then says `not_supplied`, and the successor rebuilds the written part from
mail.

**What the successor does first:**

0. Read [What the coordinator is for](#what-the-coordinator-is-for) at the
   top of this file. From now on you speak to the operator.
1. `pb worker receive`, then read the note:
   `pb coordinate project.coordinator.get --object-ref <project-ref>`, `note`.
2. Re-announce every open window from `runtime_windows` on its own channel, and
   reply to each inherited operator wait by its `message_ref`.
3. Keep the promised notifications and the merge queue as your own.
4. Check that your machine accepts project-file edits made on the card: if you
   have not coordinated from it before, ask the operator to run
   `pb host configure --add-control-kind project.file.edit` there, once
   per machine, and restart the relay. Until then the card refuses edits and
   names that command.
4. Mail addressed by name to the home coordinator while it is unavailable is
   copied to you, marked `redirected_from`. Answer it; the home coordinator
   keeps the original and sees your answer by its correlation.

The board announces every change to the team, and announces again when the
home coordinator's availability changes while you act. You do not need to tell
the team yourself.

**How to return:** the same in reverse. Write your note, then the operator
presses **Make worker** on you: the role goes back to the home coordinator and
your Card returns to the default worker profile. The home coordinator, back,
reads the note first and checks the redirected copies were answered before it
answers any original twice.

Rule 8 moves work items between workers. The role itself moves only as this
section says.

## Route

0. Read the candidates' info lines before routing: the `info_text` of each
   member in `pb worker context`, the first line on each card. Why: an agent
   publishes there what the operator told it about itself (not to be used
   actively, reviews only), and routing past it spends a quota or a session
   the operator reserved.

   **Carry an operator restriction to the card before routing from it.**
   When the operator sets or changes a restriction on a worker (a cap, a
   pause, reviews only, a resumption):
   - Record the ruling in the project Facts: what holds, who ruled, since
     when.
   - Ask that worker, when it is active, to publish the exact restriction
     with `pb worker info write` ([collaboration](collaboration.md), Rule 6,
     The info line). A paused worker is not woken for this. Until it
     returns, the Facts row is what routing reads.
   - Before you say the restriction is in place, or route from it, check
     that the worker reports its `pb worker info show` with `on_board = True`
     and that its row in a fresh `pb worker context` team shows the same
     `info_text`. You cannot run its `show` yourself; you rely on that
     reported receipt and your own fresh team read.
   - The restriction stays first on the line and every status rewrite keeps
     it. Findings and progress detail go to mail, item notes, the task and
     `pb worker busy-until`, never in place of the restriction.

   Why: on 2026-09-30 the operator let one paused agent resume under a
   weekly ceiling on its shared account's total usage. The ruling reached the
   project Facts and the team's mail, and the agent's card showed no
   restriction until the operator asked. Step 0 already said to read the
   line, so this was an execution failure. Nothing required the restriction
   to reach the card before the coordinator routed from it (W413).
1. Need, then discussion with the candidates, then decision, then route. A
   route carries the intention and the acceptance, not the engineering
   constraints. The assigned worker decides how.
2. Every ref in the route is looked up and copied whole: `project.plan.item`
   prints `identity_ref`. A ref composed from a key and a title is refused or,
   worse, accepted and wrong.
3. Name the item by key and title in every mention. A bare number is a lookup
   the reader has to make.
4. Search the plan before filing. A finding that already has an item gets a
   note on that item.
5. Set the new item's dependencies in the same call. `depends_on` lists the
   `identity_ref` of every item that must land first; an item that is related
   but does not block goes in the description as "Related:". Recheck both
   directions when you rescope an item or split out a step. Why: on
   2026-09-25 six items (W318 to W323) went out with none, and the order
   (W322 needs W323, W318 needs W313) lived only in one coordinator's head,
   which a context reset or a hand-over loses.

### Record each dispatch on the item

When you dispatch work or change its phase (an assignment, a review routing,
a poll, a handoff between agents), append one note to the item
(`plan.note.append`). It names:

- the recipient's full alias and stable worker name;
- the phase and its scope;
- the assignment or control reference and its ownership version;
- why this owner and this phase now, in one line;
- the evidence state, one of requested, queued, applied or STARTED, never
  merged into one;
- the source and job constraints;
- the next checkpoint, or that it is unknown;
- the expected deliverable and the next owner and action.

Link journal and source evidence instead of copying it into mail. The
assignment stays authoritative; the note explains the handoff. After a long
gap, read the item, its assignment and the latest handoff note before old
mail, and say which earlier instructions are superseded (W449).

### Route through Problem Board's worker CLI, with the complete assignment payload

The coordinator uses the same installed worker interface as every other
agent. `pb worker context --project-ref <project-ref>` supplies the stable
worker name, project workspace and repository evidence. `pb coordinate
project.plan.item --object-ref <project-ref> --payload-json
'{"item_key":"<Wn>"}'` supplies the current item, its complete canonical refs
and revision. The published operation procedure supplies the payload below;
`pb coordinate --help` supplies the common transport arguments. Route it with:

```bash
pb coordinate assignment.assign \
  --object-ref <project-ref> \
  --payload-file <assignment.json>
```

```json
{
  "work_ref": "<identity_ref copied whole from project.plan.item>",
  "worker_name": "<stable worker_name from pb worker context>",
  "title": "<assignment title>",
  "task": {"instructions": "<bounded briefing; the item carries acceptance>"},
  "expected_ownership_version": 0,
  "source_repositories": [
    {
      "repository_ref": "repo:<registered-alias>/<exact-relative-scope>",
      "base_commit": "<full commit>",
      "branch": "work/w<N>-<slug>"
    }
  ],
  "source_repository_ref": "<copy source_repositories[0].repository_ref>",
  "source_base_commit": "<copy source_repositories[0].base_commit>",
  "source_branch": "<copy source_repositories[0].branch>",
  "idempotency_key": "<stable key for this routing decision>"
}
```

`expected_ownership_version` is `0` only for a never-assigned item; for a move
or reissue copy the current assignment row's value. Work touching no repository
uses an explicit empty `source_repositories` and omits the one-repository mirror.
Every `repo:` value is copied from current project or item evidence. If no
authoritative read exposes it, fix that record or the CLI projection first;
never guess it from a clone name. When the operation is refused, preserve the
returned code and fields, consult the operation procedure and retry only the
documented recovery; an outcome-unknown result repeats the identical request
under the same idempotency key.

### Bind every repository the work touches when you assign

An assignment carries `source_repositories`: one entry per repository the
work touches, each with its repository ref, the base commit the worker starts
from and the branch it works on. Fill it when you assign, not later: W212
spanned two repositories and W255 three, and on 2026-09-23 every assignment
went out with the binding blank, so a reader of the board saw a worker with
open files and could not say which item they were for. Work that touches no
repository says so with an empty list. An assignment with no list reads as
`repositories not declared`, and the coordinator corrects it by reassigning
with the list.

```json
{"source_repositories": [
  {"repository_ref": "repo:kdcube-ai-app/app", "base_commit": "947238921", "branch": "work/w212-picker"},
  {"repository_ref": "repo:app-ecosystem/products", "base_commit": "83f6d21ab", "branch": "work/w212-cards"}
]}
```

## Put what a worker must read where that worker can read it

A route that points at something the assigned worker cannot open is not a
route. Before sending a pointer, ask what that worker's Card can read and what
its machine can reach.

- **Text the worker needs in order to do the work belongs in the item**, in its
  description or in the assignment's task instructions. Those travel with the
  assignment, survive an unread inbox, and every worker Card can read them
  through `project.plan.item`.
- **The current instruction lives in the item's description or task; notes
  are the history.** Worker Cards can read notes (`plan.notes.list`), but a
  worker reads the description and task first, and a note can be one of
  sixty. When an instruction changes, rewrite the description or task so it
  states what holds now, and say there which earlier briefing it supersedes;
  the note records the change and why. Never leave the current instruction
  only in a note or a mail.
- **Never send a local filesystem path as the carrier.** A path is bound to one
  machine and one user. The moment a worker runs on another host it points at
  nothing, and the failure looks like a worker ignoring instructions.
- **Files use the Board lane, not a shared filesystem.** Use `worker send
  --attach` for any worker or operator mailbox. To pass on an addressed
  message, use the exact lease-bound `worker forward` described in
  [delivery and recovery](delivery-and-recovery.md); do not copy a sender's
  local path. The normal recipient rules still apply.
- **A repository ref is portable, a working-tree path is not.** Point at a
  committed file by repository alias and path, never at `/home/...` or
  `<home>/...`.

A refusal a worker reports while following a route is the coordinator's defect
first: fix how the work was handed over, and raise the grant when the refusal
was the right rule applied to the wrong case.

## Reload, refresh, restart

Every activation is addressed to a commit: a runtime action releases the
commit its ref names, and a client switch is `pb source use-code --expect`.
The list below decides which commit that is and proves it is the one that
loaded. The commands for a runtime's actions, and what their receipts say,
are in the runtime's profile (`pb worker context`, `runtimes[].local_profile`);
this list is the same for every runtime. Nothing is ever loaded from a working
tree, which stages whatever it holds at that instant.

1. **Integrate onto the named ref first.** The action releases, in each
   repository it loads, a ref the project names for that runtime
   (`pb worker context`, `runtimes[].actions[].releases`), never a working tree. Before anything else, bring the commits that are to
   go live onto that ref, reviewed, and push it where the runtime's machine can
   fetch it; then fetch it on that machine and note the commit it names. On
   one machine this is the same step: the integrator's checkout is not the
   ref until it is pushed and fetched. On several machines, workers push their
   branches, the coordinator integrates them onto the ref, and each runtime's
   machine fetches that ref, so no machine loads another machine's working
   tree. The steps below decide nothing a working tree holds: they check that
   the commit the ref names is the one to release, and prove it loaded.
2. **Read the dashboard first**, map every row to its concrete worktree or
   runtime boundary, and act on that relationship. The row says what a worker
   is about to change and `git status` says what has changed, and neither makes a
   hold by itself. A commit-addressed action stages the approved commit from
   its clean release tree. A `source_in_flight` row for an isolated worker
   worktree is therefore informational: that worktree cannot
   change the candidate, so record its owner in the announcement and proceed.
   The row holds the action only when at least one of these is true:

   - the worker can write the same filesystem tree the action will stage, and
     that tree is dirty or can still change after preflight
   - the approved candidate is meant to include the worker's in-flight commit,
     but that commit has not been integrated onto the released ref
   - the runtime action would interrupt or conflict with the worker's current
     local or runtime operation

   For a real hold, ask its owner for the release condition, record it, and
   wait for that condition. A `reload` row requests activation of its named
   commit. Apply the same three conditions to it and proceed when none applies.
   A worker answers the announcement with the same three conditions
   ([test-window](test-window.md)). Why: every worker develops in its own
   worktree, so only a shared tree, a missing commit or a running operation can
   change what the action loads.
3. **Announce** the action, the tree, the approved commit per tree (full
   sha: that commit, not the tree, is what the action loads), and what it
   releases (a worker may
   have published the activation it asks for as a `reload` row with
   targets `bundle:<id>` and `procedure:<package>@<revision>`, so the
   others see a reload coming, and which revision, before the mail): the commits since
   the last activation of that tree (`git log <last>..HEAD -- <tree>`), one
   line each, naming any that are unreviewed. Collect one `ready` or `hold`
   from every attending worker. One `hold` stops it. A `ready` that carries a
   constraint (a commit it must be at or after, a window it needs, a file it
   is about to touch) is honoured or the action is re-announced. A `hold`
   names what releases it (a commit, a clear, a time) and the holder sends
   the release. A hold without a condition is asked for one, and when its
   condition is met and verified (the named commit on `HEAD`, the row's paths
   clean) the action may run with the hold quoted. A worker that has not
   answered is asked once more with the deadline. Running without its answer
   is allowed when the exact release tree is clean at the approved
   commit and the worker's dashboard row maps to another isolated worktree or
   otherwise meets none of the three hold conditions above. The announcement
   records the missing answer, the mapped worktree or runtime boundary, and
   that reason.
4. **Immediately before**: `git status --porcelain` on the tree and a
   `pb worker receive`. Both are evidence about that moment and neither is the
   guarantee: the tree can change between the check and the staging, and the
   guarantee is an activation addressed to a commit. Run the checks the
   runtime's profile adds for this moment (a descriptor behind the change,
   for example).
5. **Execute** the action as the runtime's profile gives it, at the commit
   the ref names, in the order the profile gives when one window moves more
   than one tree; for the host's client, `pb source use-code` with `--expect`.
   Then **check
   the receipt against the approved candidate**: the profile says which line
   of the receipt names what loaded, and the relay's first stamped line
   (`source=snapshot`, `app_ecosystem=<sha>`) names the client. A receipt that names another commit is a failed
   activation: report it as failed, with both commits, and stop there. An
   action that returns before its build finishes has not said whether the
   artifact is current; only step 6 does.
6. **Verify the deployed artifact, never the commit.** Ask the running
   process for one symbol or behaviour the change introduced, with the checks
   the runtime's profile gives. Relay: the first stamped line of the new pid
   (`file_descriptor_limit=`, `source=`). Package: `pb procedure verify` on
   the host. For a relay, identity is not enough: once the longest designed
   interval has passed after activation (the 900 s workspace walk today),
   compare the relay log with the same length of log before it. Count each
   periodic job's lines against its designed interval, and the slow channel
   turns. A periodic job that runs more often than designed, or a slow-turn
   rate several times the earlier window's, is a failed activation: report
   both counts, hold the ALL CLEAR and keep the rollback. Why: on 2026-10-02 an
   activation passed identity proof while its workspace walk ran on every
   heartbeat (278 walks in 48 minutes for 3 channels against one per 900 s
   each), readable from the release's own log lines that nobody compared.
7. **Say what loaded**: the pid or the profile's receipt, the ref and the
   commit it named, the commit range, and, for
   each worker whose commits rode along, that they did. Clear your dashboard
   row. A worker asking "what did that release" is asking for this line.
8. **Bump the app's release record.** A board release updates the app's
   `release.yaml` in the same change as the release: its version and a dated
   note naming what the release carries. A release record left behind tells
   nobody what runs.

**New operations reach an agent only through the project Control Card.** A
project's Control Card caps every agent Card in the project (AND), and a Card
Refresh is capped by it too. After new operations reach the catalog, a project
admin ticks them on the project Control Card in Connection Hub first, and
only then refreshes Cards. A refusal that names the Control Card
(`work_worker_operation_withheld_by_control_card`) means exactly that step is
missing: re-consent and re-publishing the catalog do not help. Why: on
2026-09-26 a coordinator refresh after new operations reached the catalog
changed nothing until they were ticked on the project Control Card.

A LaunchAgent or systemd unit is host service configuration: `pb relay-service
install` is typed by an agent after the operator approves it, so it is theirs
to clear, as is any action the runtime's profile says rebuilds their stack.
Restarting a service whose definition exists is a coordinated runtime action
and needs no more than the list above.

`pb source use-release` and `pb source use-code` change the immutable source
used by both the host command and that service, and include the restart needed
for the relay to observe it. Announce the exact package version or, for code,
the full App Ecosystem commit. Collect the same host-local
readiness, and verify `pb source status` plus the new relay startup record
before reporting the move complete. `pb procedure install` installs the
procedure package of the release `pb` runs, so a procedure change merged on
`main` reaches a host only after its source moves to a commit that contains
it: switch the source, then install for each target, then `pb procedure
verify` (2026-10-02, W455).

When a host still runs the checkout client, follow the cutover section in
[runtime actions](runtime-actions.md) before fast-forwarding that checkout.
The four-package source install, commit verification, and relay restart are one
precondition for the fast-forward, not recovery steps after it.

## Research Is Coordinated Progressively

The coordinator names one researcher for a question and tells the other workers
who owns it; others continue their assigned work. The researcher returns concise
findings with a `repo:<alias>/<path>` link per source plus line or symbol
detail. Receivers assess them before building on them and ask the researcher for
targeted verification of an uncertain fact. A second investigation starts only
when the coordinator or operator names a specific reason. (Moved here from the
skill in revision 2026.09.23.3 to make room for the brief-output rule: content
moves, prose is not compressed.)
