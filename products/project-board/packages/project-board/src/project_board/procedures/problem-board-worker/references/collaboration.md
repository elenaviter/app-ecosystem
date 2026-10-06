---
id: project-board.skill-reference.collaboration
title: Collaborate Across Agents And Machines
summary: How several agents on several machines work on the same repositories without blocking or overwriting each other, exchange work through change requests, and integrate through the coordinator. Written to be run, observed and revised, one rehearsal round at a time.
tags: [procedure, problem-board, collaboration, git, change-request, coordinator, multi-machine]
keywords: [host without gh, one tree per agent, work branch, change request, pull request, merge gate, integration ref, intent before edit, shared write, coordinator decides, rehearsal round, collision log]
see_also:
  - ./coordinator.md
  - ./runtime-actions.md
  - repo:app-ecosystem/products/project-board/packages/project-board/src/project_board/procedures/agent-worker.md
  - repo:app-ecosystem/products/project-board/packages/project-board/src/project_board/procedures/operator.md
  - repo:app-ecosystem/products/project-board/packages/project-board/src/project_board/procedures/add-a-worker-host.md
---

# Collaborate Across Agents And Machines

**Status:** first pass, 2026-09-22 (W262). Usable, not complete. It is a
loop, not a document: the team works under it, what collides is recorded in
the project's journal, and the rules change on what was observed. The measure is the count of collisions and mistakes going down from
one round to the next.

**Written for:** several agents on several machines: a shared host where
several agents work beside the runtime, and further hosts added with
`procedures/add-a-worker-host.md`. Nothing below assumes one checkout, one
host, or that two agents can talk to each other in real time. One machine is a
fair test of every rule except the cross-host paths, so the loop starts on the
shared host before a second host is authorized.

## Modules

This reference is split into modules (W563): read the module whose act you are doing, whole; a changed procedure revision names only the modules that changed (`pb procedure verify`, `changed_files`).

| Module | What it covers |
|---|---|
| [What goes wrong without it](collaboration/what-goes-wrong-without-it.md) | All of these happened on 2026-09-22, on one machine, in one evening: files sat in the same working tree (one agent's W253, blocked by another's W272). |
| [Rule 1. One working tree per agent](collaboration/rule-1-one-working-tree-per-agent.md) | Every agent works in its own working tree: a separate clone or a `git worktree`, in its own workspace folder. |
| [Rule 2. Work on a branch, exchange through a change request](collaboration/rule-2-work-on-a-branch-exchange-through-a-change-request.md) | Each item is worked on a branch of the repository, pushed by its author, and exchanged as a change request against the integration ref. |
| [Rule 3. Make your intent visible before you edit](collaboration/rule-3-make-your-intent-visible-before-you-edit.md) | Before the first edit for an item, publish what you are about to touch where every agent can read it, and read what the others have published. |
| [Rule 4. Where two changes meet, the coordinator decides](collaboration/rule-4-where-two-changes-meet-the-coordinator-decides.md) | A decision that needs two agents' changes to agree goes to the coordinator, with both sides named, and the coordinator decides. |
| [Rule 5. The merge gate](collaboration/rule-5-the-merge-gate.md) | The merger the item's route names (Rule 16), or the coordinator when none is named, merges a change request when all of these hold, and refuses it naming the one that does not. |
| [Rule 6. Your visible state says where you are and what you are on](collaboration/rule-6-your-visible-state-says-where-you-are-and-what-you-ar.md) | At any moment another agent, the coordinator or the operator can read, from the board alone: which item you hold, which branch and change request carry it, what you touched (Rule 3), and what you are waiting on. |
| [Rule 7. A question about how the team collaborates is decided in rounds](collaboration/rule-7-a-question-about-how-the-team-collaborates-is-decided.md) | When the team has to choose how it collaborates or stays visible (a practice, a channel, a mechanism that changes what each member publishes), one member proposes and runs the decision. |
| [Rule 8. Handoff is an ownership decision, not a note](collaboration/rule-8-handoff-is-an-ownership-decision-not-a-note.md) | Work moves from one worker to another only by a new ownership version on the assignment, and the coordinator issues it. |
| [Rule 9. What is published is safe to publish](collaboration/rule-9-what-is-published-is-safe-to-publish.md) | Everything a worker publishes for another reader (a status, a resume record, a handoff, observed paths, a WIP push) carries stable refs, hashes and redacted evidence, and never a secret, a bearer cred |
| [Rule 10. A runtime window speaks one channel that survives it](collaboration/rule-10-a-runtime-window-speaks-one-channel-that-survives-it.md) | While the platform is down for an apply or a migration, the board is unreachable, so nothing said during the window reaches a worker on another machine. |
| [Rule 11. The operator is asked on the board, and on Telegram when it is urgent](collaboration/rule-11-the-operator-is-asked-on-the-board-and-on-telegram-w.md) | **Anything that waits on the operator is a work item assigned to them.** Whoever needs the operator, worker or coordinator alike, for a test, a decision, an approval, a choice or an acceptance, puts i |
| [Rule 12. Cards are edited only in Connection Hub](collaboration/rule-12-cards-are-edited-only-in-connection-hub.md) | An application links to a Card in Connection Hub, optionally with a preselection or a focus for the purpose the link serves. |
| [Rule 13. A behaviour change carries its documentation in the same change](collaboration/rule-13-a-behaviour-change-carries-its-documentation-in-the.md) | Every change to Problem Board behaviour updates the public documentation (`repo:app-ecosystem/products/project-board/docs/`) in the same change; when the code lives in a private repository, the docume |
| [Rule 14. Search before you file an item](collaboration/rule-14-search-before-you-file-an-item.md) | A worker's Card may create plan items (`plan.item.create`). |
| [Rule 15. A shared name or field is settled in one exchange](collaboration/rule-15-a-shared-name-or-field-is-settled-in-one-exchange.md) | When two agents must agree on a name, a field or a message shape that both sides use, the owner of the side where it lives proposes it once, complete, and the other adopts it word for word or says wha |
| [Rule 16. Every task has a living route, and each actor knows its next step](collaboration/rule-16-every-task-has-a-living-route-and-each-actor-knows-i.md) | Every task carries, on its item, a route from planning to acceptance that every participant can read, kept whole through every reprioritization: the deliverable and its scope, the acceptance, the curr |
| [From this moment: round 2 on the shared host, 2026-09-22 22:20Z](collaboration/from-this-moment-round-2-on-the-shared-host-2026-09-22-2220z.md) | Round 1 ran four hours under rules that did not exist when it started, and changed four of them from eight findings. |
| [Interim, 2026-09-22 22:20Z: what is true now versus the target](collaboration/interim-2026-09-22-2220z-what-is-true-now-versus-the-target.md) | Historical record of that night, kept for its findings. |
| [Rehearsal log](collaboration/rehearsal-log.md) | One entry per round. Record what collided, who was blocked, what nobody could see, and what changed in this procedure because of it. |
