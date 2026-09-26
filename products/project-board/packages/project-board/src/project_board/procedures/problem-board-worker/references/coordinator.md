---
id: project-board.worker-reference.coordinator
title: Accept, Route, Reload, Refresh
summary: The coordinator's checklist for review decisions, capacity-aware routing, teammate setup, shared project knowledge, and runtime actions, placed where each act happens so the rule is present when it is applied.
tags: [procedure, problem-board, coordinator, review, routing, runtime]
keywords: [what the coordinator is for, speak to the operator, coordinator duties, review.accept, coordinator handover, handover note, make coordinator, make worker, recipient coordinator, assignment.return, release assignment, worker budgets, token budget, machine-local resources, provider quota pool, teammate setup, project journal, project facts, project environment, idempotency_key, work_review_self_forbidden, route, discuss before routing, shared-write dashboard, ready or hold, hold release, missing answer, preflight, bundles.template.yaml, bundle reload, refresh --build, widget build states, verify the artifact, what loaded]
see_also:
  - runtime-actions.md
  - test-window.md
---

# Accept, Route, Reload, Refresh

Read this when you are about to accept, return or cancel a submission, release
a stalled assignment, route an item, or reload, refresh or restart the runtime. Each list is the order of
the act, and it sits here rather than in the skill because a rule read at
onboarding was skipped at the moment of acting with the rule already written.

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
- Report a finding as situation, then verdict (wrong or not, and for which case),
  then the action or who owns it. Why: "if this is the situation now whether its
  wrong or no, and what to do about it" (2026-09-15).
- Name items by key and title, never a bare number. When you ask the operator to do
  something in the product, name the control they see on their screen, not the
  operation behind it. Why: an instruction in the API's words sent the operator looking for
  a button that does not exist (2026-09-20).

**You drive the team; you do not wait for it.**

- Know what every worker is doing. When a reply is overdue (about ten minutes, or
  any window waiting on one worker), check its state yourself: its relay wake
  lines, its queue, its heartbeat. Then re-send, nudge, or tell the operator what
  is stuck. Why: "look on the status of workers after you wait for long time"
  (2026-09-23).
- Ask the worker; do not infer from files. Decide what is yours to decide; hand
  the operator only what is theirs. Why: "cant you ask?" (2026-09-15).
- Before routing, discuss the need with the candidates, then decide, then route.
  A brief carries the intention and the need; the worker derives the constraints.
- Let a worker finish its current step before switching it; queue the next thing.
  An operator's remark about what a worker is doing is information, not an order.
- When a worker runs short of tokens, move its unstarted work to agents with budget
  left, without being asked ([Worker budgets](#worker-budgets), below).
- When you diagnose why something failed for a worker, tell that worker first, then
  the operator. Why: otherwise it repeats the failure.
- Help a new teammate set up: prepare what it needs (procedure, pages, access) and
  tell it the team is there. Put project knowledge in the journal, not in mail.

**The runtime is the operator's; the mechanics are yours.**

- No runtime window (reload, refresh, client switch) without the operator's go.
  Once the operator gives it, you run the whole window yourself: announce, collect ready,
  back up the board tables, execute, verify, report, update the facts table. Do not
  ask the operator about the mechanics.

## Accept, return, cancel

1. The submission is read against the item's acceptance lines, one by one, and
   against the deployed artifact where a line is about behaviour (see Reload
   below for what "deployed" means per tree).
2. A pull request to the applications repository is accepted after the app's
   Python suite runs on `main` with the change merged, widget-only changes
   included: the contract tests read widget source. A failure is compared
   against the same suite on `main` before the change.
3. A submission that closes a line with "that case cannot occur" is returned,
   not accepted, until it names the observation that would show the case
   occurring. A claim is derived from the observation that would falsify it
   and carries that observation's timestamp. A name, a timer or a threshold is
   not one.
4. `review.accept`, `review.return` and `review.cancel` take the item
   `work_ref` looked up from `project.plan.item`, its `expected_revision`, and
   an `idempotency_key` you generate for this decision. Return and cancel take
   a reason. The service refuses the worker that submitted the work from
   deciding on it (`work_review_self_forbidden`), for all three decisions.
5. The item is the record. After the decision, read the item back: status
   `done` for accept, `todo` with the same assignee for return (the worker
   keeps the assignment, its ownership version advances, and it reworks
   against the new version), `cancelled` for cancel, and the assignment state
   beside it. To hand returned work to someone else, release it with
   `assignment.return` and its reason, then assign. Mail about the decision is
   commentary.

6. **Route reviews (W326).** An item that enters Review with no reviewer
   named comes to you as the acting coordinator, with a review request in
   your inbox. Review it yourself, or name who does with `review.assign`
   (`pb coordinate review.assign --object-ref <project> --payload-json
   '{"work_ref": "<item ref>", "reviewer": "<stable worker name>"}'`): an
   agent linked to the project other than the one who did the work, or
   `operator` with `"integration": {"merged": [...], "deploy": "<window>:
   <check>"}` (or `"nothing_to_deploy": true`) once it is merged and deployed.
   The board refuses the operator without that evidence and names what is
   missing. Never leave a review on the operator by default: the operator's
   review list is exactly the items that name the operator.

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
operator says. At project start, and whenever hosts, workers, capabilities or
accounts change, keep a small routing inventory in the project's facts or
environment page:

- for each host, the machine-local resources and capabilities, and the workers
  that can act as hands on that host;
- the workers that share a provider account or quota, grouped as one quota
  pool, with its reset time when known. Their limits are coupled, not
  independent capacity. Record `Not known yet` instead of assuming that two
  workers have independent limits.

Use only this routing heuristic:

1. **Locality required?** Route work that requires a machine-local resource to
   a worker with hands on that host. Reserve scarce host-local workers and
   enough of their quota pool for that work; do not spend the last capable
   local worker on portable work.
2. **Independent quota available?** Route portable work, including review,
   research and planning, to another host or an independent quota pool first,
   subject to the skills and access the work needs.
3. **Reset soon enough?** When a host-local worker or its quota pool is running
   short and the reset is not soon enough for the work, replan before
   exhaustion. Move unstarted portable work, leave the scarce worker only the
   cheap or locality-required steps it can finish, and hand over the exact
   branch and head. If the work can safely wait for an imminent reset, wait
   instead of churning ownership.

Do not build a scheduler or assign token scores. The routing inventory and
these three questions are the whole rule.

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

## Keep everything known in the project journal

The project journal home is the team's complete shared record. It carries the
facts page, the environment page, operator rulings with their reasons,
runtime-window outcomes, and every project-wide gap with its fix. Whoever
learns a project-wide fact writes a journal entry and points to it from the
relevant item note or mail thread. The coordinator checks that the entry exists,
and a successor coordinator begins by searching the journal.

Attending agents find this record with `pb worker journal-search`. A work-item
note or mail thread can carry the immediate conversation; its journal link
makes the resulting knowledge available to the whole team and to later
sessions.

### Starting a project

The journal accumulates from the first day. When the journal home is bound at
project creation, the coordinator creates `project-facts.md` and
`project-environment.md` with their standard sections. Every section contains
either the current fact or `Not known yet`. The first host, repository,
environment, and operator ruling update those pages as soon as each becomes
known.

Automatic page seeding by the board is a product follow-up. The coordinator
owns this creation step in the current procedure.

## Check a silent worker, do not wait for it

When a reply you are waiting for is overdue (a `ready`, a change request
head, a result), check the worker's state yourself. Do this after about ten
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

## Stay reachable through every window

The coordinator's own notification path is armed at all times:

- **Before the watch expires:** re-arm it before or at its expiry notice, including during a runtime window. When the relay is down the watch reports nothing, and that is fine. What must not happen is a relay that comes back to a coordinator nobody can wake.
- **Every wait ends:** anything you wait for (a result file, a relay restart, a worker's reply) runs as a background check with a bounded end, so its completion or its timeout wakes you. A wait with no check behind it is time nobody accounts for.
- **After every window:** once the relay is back, receive immediately, before the all-clear, and verify each worker's wake was pushed (see the section above).

Why: on 2026-09-24 the coordinator's watch expired during a window and was not re-armed after the relay returned. Only an unrelated background check woke it. The operator: "if i did not write to you now that would stand still forever?"

## Hand the coordinator role over, and take it back

The role is held by one agent at a time, the holder. The home coordinator keeps
its label while another agent acts. Mail for whoever coordinates goes to
`--recipient coordinator` with `--project-ref`: the board resolves it to the
holder at send time, so procedure text and teammates address the role, never
the agent holding it today.

**When to hand over:** your runtime reports a limit coming (`pb worker
limit-state`, W26), a planned absence, the operator asks, or the relay reports
you out of tokens. A successor that must run a runtime window has to be able to
deploy: on this team that is an agent on dev-main.

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
- **Notes are the record, not the briefing.** Worker Cards do not grant
  `plan.notes.list` today, so "read the newest note" refuses. Decide by what the
  Card grants, not by what the coordinator can see.
- **Never send a local filesystem path as the carrier.** A path is bound to one
  machine and one user. The moment a worker runs on another host it points at
  nothing, and the failure looks like a worker ignoring instructions.
- **Attachments are operator-only.** Worker-to-worker mail refuses them with
  `field_attachments_operator_only`; local worker mail carries paths in its
  body, which is subject to the rule above.
- **A repository ref is portable, a working-tree path is not.** Point at a
  committed file by repository alias and path, never at `/home/...` or
  `<home>/...`.

A refusal a worker reports while following a route is the coordinator's defect
first: fix how the work was handed over, and raise the grant when the refusal
was the right rule applied to the wrong case.

## Reload, refresh, restart

Every activation is addressed to a commit: an app reload from its deploy
worktree checked out at that commit, a refresh from clean exports of named
commits, a client switch with `pb source use-code --expect`. The list below
decides which commit that is and proves it is the one that loaded. An app whose
path is a working checkout stages that tree at the instant of the reload,
whatever it holds, so no app's path is ever a working checkout.

1. **Integrate onto the named ref first.** The action releases a ref the
   project names for that runtime (`pb worker context`, `runtimes[].actions[].from_ref`),
   never a working tree. Before anything else, bring the commits that are to
   go live onto that ref, reviewed, and push it where the runtime's machine can
   fetch it; then fetch it on that machine and note the commit it names. On
   one machine this is the same step: the integrator's checkout is not the
   ref until it is pushed and fetched. On several machines, workers push their
   branches, the coordinator integrates them onto the ref, and each runtime's
   machine fetches that ref, so no machine loads another machine's working
   tree. The steps below decide nothing a working tree holds: they check that
   the commit the ref names is the one to release, and prove it loaded.
2. **Read the dashboard first**, and act on each row. The row says what a
   worker is about to change and `git status` says what has changed. A
   `source_in_flight` row with targets under the tree you are about to
   stage, and a tree that is dirty anywhere, holds the action until that
   worker commits or clears, whether or not git shows the named path yet:
   the write git cannot see yet is the one you can still avoid staging. The
   same row with a clean tree is a worker that has declared and not begun,
   and a reload then stages committed state only: ask its owner, now or
   after, and act on the answer, because the holder decides its own hold.
   Name the row's owner in the announcement either way. A `reload` row is
   a request and never a hold: a `source_in_flight` row says do not stage
   me and a `reload` row says please stage me, and a coordinator that
   treats every row as a hold is blocked by the request asking it to
   proceed.
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
   is allowed only when its dashboard row and `git status` both show nothing
   of that worker's under the tree being staged, and the announcement records
   the missing answer and that reason.
4. **Immediately before**: `git status --porcelain` on the tree and a
   `pb worker receive`. Both are evidence about that moment and neither is the
   guarantee: the tree can change between the check and the staging, and the
   guarantee is an activation addressed to a commit. When the range touches
   `bundles.template.yaml`, diff the touched entry against the live
   `config/bundles.yaml` first: a fix whose descriptor is behind it deploys
   and cannot run. Identical blocks sit under different bundle ids in that
   file, so edit the live descriptor by locating the bundle id, never by the
   first match of a block.
5. **Execute** the action `runtime-actions.md` names for the tree, at the
   announced commit: for an app, first read its entry in the staged
   `config/bundles.yaml` (located by bundle id) and remove any `activation`
   block, `commit` or `require_commit`, because a commitless reload still
   applies it in the web proc while the Data Bus workers load the deploy
   worktree, and `--local-path` keeps it; then `git -C <deploy-worktree>
   checkout --detach <sha>` and `kdcube bundle reload <bundle-id>`; `kdcube refresh --build`
   from exports of the announced commits; `pb source use-code` with `--expect`
   and `--expect-kdcube`. Why the deploy worktree: it is the app's only path,
   read by web requests, the Data Bus workers and a restart alike, and nobody
   edits it, so the commit checked out there is what every process loads and a
   restart keeps it. The working checkouts are never an app's path. The
   descriptor's `activation.commit` is not the guarantee: a restart and the
   Data Bus workers ignore it (W333), and the 2026-09-25 23:23Z window removed
   it. **When one window refreshes the platform and moves an app**, check the
   app's deploy worktree out at its approved commit **before** `kdcube refresh
   --build`, then refresh. The refresh restarts the process, and the process
   loads the app from its path at startup. A bundle reload afterwards evicts the
   bundle but not submodules already cached, so the process can run new code
   against old modules. That happened on 2026-09-25: the board failed with
   `ImportError: card_delegable_grants` from 11:23 to 11:27Z, until a restart
   (W304 U3).
   Then **check the receipt against the approved candidate**: for an app
   the reload's line reads `Loaded: mounted tree at head <sha>, clean`, a
   `Loaded: snapshot of` line is a failed activation because a pin is still in
   effect, and `git -C <deploy-worktree> rev-parse HEAD` is the commit on disk;
   the
   relay's first stamped line (`source=snapshot`, `app_ecosystem=<sha>`), and
   the commits the refresh exported each equal the announced commit. A receipt that names another commit is a failed
   activation: report it as failed, with both commits, and stop there. A bundle
   reload returns before the widget build finishes, and a widget has three
   states after a reload: build pending, no build because the signature was
   unchanged and the artifact is already current, and no build because it
   broke. The receipt does not tell them apart, only step 6 does.
6. **Verify the deployed artifact, never the commit.** Bundle: the eviction
   count plus one symbol or behaviour the change introduced, asked of the
   running process. Widget: `dist/` inside the container carries the new
   source, after the build ends. Relay: the first stamped line of the new pid
   (`file_descriptor_limit=`, `source=`). Package: `pb procedure verify` on
   the host. Descriptor: `bundle status` on the running catalog.
7. **Say what loaded**: the pid or eviction count, the commit range, and, for
   each worker whose commits rode along, that they did. Clear your dashboard
   row. A worker asking "what did that release" is asking for this line.

A LaunchAgent or systemd unit is host service configuration: `pb relay-service
install` is typed by an agent after the operator approves it, and
`kdcube refresh --build` rebuilds the operator's stack, so both are theirs to clear.
Restarting a service whose definition exists is a coordinated runtime action
and needs no more than the list above.

`pb source use-release` and `pb source use-code` change the immutable source
used by both the host command and that service, and include the restart needed
for the relay to observe it. Announce the exact package version or, for code,
the full App Ecosystem and KDCube commits. Collect the same host-local
readiness, and verify `pb source status` plus the new relay startup record
before reporting the move complete.

When a host still runs the checkout client, follow the cutover section in
[runtime actions](runtime-actions.md) before fast-forwarding that checkout.
The six-package source install, two-commit verification, and relay restart are one
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
