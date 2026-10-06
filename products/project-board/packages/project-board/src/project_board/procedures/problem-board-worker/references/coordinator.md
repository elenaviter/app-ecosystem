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
| resume after a compaction, or hand a session's state on | [Write the recovery handoff](#write-the-recovery-handoff) |

Read [What the coordinator is for](#what-the-coordinator-is-for) once when
you take the role; the rest only for the act at hand.

## Modules

This reference is split into modules (W563): read the module whose act you are doing, whole; a changed procedure revision names only the modules that changed (`pb procedure verify`, `changed_files`).

| Module | What it covers |
|---|---|
| [What the coordinator is for](coordinator/what-the-coordinator-is-for.md) | The coordinator works for the operator. |
| [Refresh the evidence you decide from](coordinator/refresh-the-evidence-you-decide-from.md) | Freshness belongs to the decision boundary, not to the session. |
| [Accept, return, cancel](coordinator/accept-return-cancel.md) | 1. The submission is read against the item's acceptance lines, one by one, and against the deployed artifact where a line is about behaviour (see Reload below for what "deployed" means per tree). |
| [Release a stalled assignment](coordinator/release-a-stalled-assignment.md) | 1. `assignment.return` (Release assignment) takes the item `work_ref` from `project.plan.item`, its `expected_revision`, a reason, and an `idempotency_key` you generate. |
| [Worker budgets](coordinator/worker-budgets.md) | The coordinator treats every worker's usable token budget as routing state. |
| [Set a teammate up to work](coordinator/set-a-teammate-up-to-work.md) | The team prepares the setup a teammate needs. |
| [Keep what the project learned in the journal](coordinator/keep-what-the-project-learned-in-the-journal.md) | The project files are the team's current truth, and every project has them (project workspace, "Project files"). |
| [Plan every batch with a roles table, and dispatch from it](coordinator/plan-every-batch-with-a-roles-table-and-dispatch-from-it.md) | A batch is any coordinated set of work: a half-day plan, a release, an incident, a runtime window. |
| [Reconcile the work, not the inbox](coordinator/reconcile-the-work-not-the-inbox.md) | Read the team's progress from the assignments, never infer it from whichever mail reached you. |
| [Confirm that work started](coordinator/confirm-that-work-started.md) | STARTED is the worker's `working` report at the current ownership version. |
| [Check a silent worker, do not wait for it](coordinator/check-a-silent-worker-do-not-wait-for-it.md) | When a reply you are waiting for is overdue (a `ready`, a change request head, a result), or an assignment or a review has no reported start, check the worker's state yourself. |
| [Name who a wait is on, and receive before you repeat it](coordinator/name-who-a-wait-is-on-and-receive-before-you-repeat-it.md) | Every status that says work is waiting or blocked names four things: named person (a credential only they hold, consent on their own account). |
| [Keep the project announcement current](coordinator/keep-the-project-announcement-current.md) | The board shows the project's announcement above the plan, so the operator and the team see where the project stands without asking you again (operator, 2026-09-30). |
| [Stay reachable through every window](coordinator/stay-reachable-through-every-window.md) | The coordinator's own notification path is armed at all times: Why: on 2026-09-24 the coordinator's watch expired during a window and was not re-armed after the relay returned. |
| [Recover a stalled Codex delivery](coordinator/recover-a-stalled-codex-delivery.md) | A running relay and an active Card prove transport, not that a model received its mail. |
| [Write the recovery handoff](coordinator/write-the-recovery-handoff.md) | A recovery summary (the one a runtime writes at a compaction, or one you write for a successor session) carries the state still open, in at most 1,500 words, and refs to everything else (operator, 2026-10-05, W563 Q6). |
| [Hand the coordinator role over, and take it back](coordinator/hand-the-coordinator-role-over-and-take-it-back.md) | The role is held by one agent at a time, the holder. |
| [Route](coordinator/route.md) | 0. Read the candidates' info lines before routing: the `info_text` of each member in `pb worker context`, the first line on each card. |
| [Put what a worker must read where that worker can read it](coordinator/put-what-a-worker-must-read-where-that-worker-can-read-it.md) | A route that points at something the assigned worker cannot open is not a route. |
| [Reload, refresh, restart](coordinator/reload-refresh-restart.md) | Every activation is addressed to a commit: a runtime action releases the commit its ref names, and a client switch is `pb source use-code --expect`. |
| [Research Is Coordinated Progressively](coordinator/research-is-coordinated-progressively.md) | The coordinator names one researcher for a question and tells the other workers who owns it; others continue their assigned work. |
