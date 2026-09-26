---
id: project-board-operations-by-actor
title: Operations By Actor
summary: Every Problem Board operation with the service permission it needs, and whether a worker, a coordinator and a person operating the project should hold it on their Card, with the reason.
tags:
  - project-board
  - authorization
  - operations
  - cards
keywords:
  - operation catalog
  - Card grants
  - worker Card
  - coordinator Card
  - person Control Card
  - project Control Card
  - work:relay
  - work:coordinate
  - work:observe
  - work:review
  - work:admin
  - least privilege
see_also:
  - ./README.md
  - ./cards.md
  - ./review.md
  - ./architecture.md
  - ./coordinator.md
---

# Operations By Actor

Every action on the board is an operation. A caller may perform an operation
only when its Card holds it (the operator's ruling of 2026-09-20: "it is only
permissions based"). This page lists every operation, the service permission it
needs, and which actor should hold it and why, so a Card can be ticked
deliberately instead of all at once.

How a call reaches the board with the caller's Card is in
[Architecture](architecture.md). Who edits which Card is in [Cards](cards.md).

## The actors

| actor | who | Card |
| --- | --- | --- |
| **worker** | a coding-agent session doing assigned work | its own caller Card |
| **coordinator** | an agent session that routes, reviews and closes work | its own caller Card |
| **operator** | a person who owns or operates the project | the person's Control Card for the project, which the board keeps for the signed-in person |

The **project Control Card** is the project's ceiling. A caller Card linked to
it can use only what both allow (the AND/OR rule is on the Control Card). For
now the Control Card has every operation ticked, so the caller Cards decide.

Who edits which Card in a project (project admins by role, members only their
own My Card) is in [Cards](cards.md).

## Service permissions

These are the six chips under **Service permissions** on a Card. Each
operation below declares which of them it needs.

| permission | label on the Card | what it covers | worker | coordinator | operator |
| --- | --- | --- | --- | --- | --- |
| `work:observe` | Observe project boards | Read project plans, worker presence, and service history. | yes | yes | yes |
| `work:relay` | Operate a Problem Board worker relay | Operate one session-bound worker channel: publish its identity, discover its current project, pull addressed controls, publish sanitized state. | yes | yes | no |
| `work:journal:view` | Serve requested project journals | Let the relay on a worker's host return a requested journal page or entry through an expiring view. | yes | yes | no |
| `work:coordinate` | Coordinate project workers | Register projects, bind journal homes, manage worker attendance, assign and release work, suspend workers, enqueue controls. | yes, for the ticked operations only | yes | yes |
| `work:review` | Review submitted work | Accept, return, or cancel work in review. | no | yes | yes |
| `work:admin` | Administer project people | Invite people, apply role presets and decide what each person's project Card holds. | no, unless its owner ticks it | no, unless its owner ticks it | on an admin's project Card |

A worker needs `work:coordinate` only because a few operations it must have
sit under it. For a worker the **ticked operations** are the real limit: tick
these three, and beyond them only what the operator chooses to add.

| operation | why a worker has it |
| --- | --- |
| `work.status.set` | It sets the status of the work it is doing. |
| `plan.note.append` | It writes its findings onto the item it is assigned. Granted to every agent on 2026-09-23, after a worker with an open assignment was refused it and had to route its finding through the coordinator, which made the record depend on the coordinator being awake. |
| `plan.item.update` | It corrects the wording of the item it works on. Granted to every agent on 2026-09-22, with `plan.notes.list`. |

Two more sit under `work:coordinate` and are optional for a worker, by the
operator's decision rather than by default: `plan.item.create`, when workers
file their own findings, and `control.enqueue`, when a worker directs other
workers. The operations table marks both.

This list and the **worker** column of the operations table are the same
statement written twice; when they drift, the grant-level row is the one
people read first. A narrower permission that holds these three without the
rest of `work:coordinate` is an open question.

## How to read the table

- **yes**: the actor needs it for its normal role.
- **optional**: give it only when that actor is meant to take on this part.
- **no**: the actor should not hold it. A worker does not hold operations that
  remove, suspend or re-route other workers, or that close work. Directing
  other workers with `control.enqueue` is optional.
- The **permission** column is the service permission each operation declares
  in the app descriptor. On the Card screen these are the **Service
  permissions** chips. An operation works only when the Card holds both its
  service permission and the operation itself (the **Tools**).

## Operations

An operation in this table is available to an agent only when its **Card**
carries it. The table states what the role may hold; a Card issued with a
narrower selection refuses the operation with `work_worker_operation_not_granted`
even though the row says yes, and gaining it needs a re-consent for that Card.
On 2026-09-22 one worker's Card was missing `plan.notes.list` and
`plan.item.update` while its peers held both. On 2026-09-23 a worker holding an
open assignment was refused `plan.note.append`. In both cases the row said yes
and the Card did not, which is why a refusal is read against the Card and not
against this table.

An agent's Card is also capped by its project's **Control Card** (AND): an
operation the project Control Card does not include is refused with
`work_worker_operation_withheld_by_control_card`, which names that Control
Card, whatever the agent's own Card holds. Re-consent does not help. A project
admin adds the operation to the project Control Card in Team > People, then the
agent's Card is refreshed (Refresh coordinator Card, Make coordinator or Make
worker). On 2026-09-26 new coordinator operations reached the catalog, and
`project.coordinator.note.write` stayed refused after Refresh until the operator
ticked it on the project Control Card; the refusal then still read "written
at consent", which sent the team to the catalog.

| operation | permission | worker | coordinator | operator | what it does | why |
| --- | --- | --- | --- | --- | --- | --- |
| `project.register` | `work:coordinate` | no | optional | yes | Register a project under the signed-in owner. | An owner decision. A coordinator may do it on the owner's instruction. |
| `project.set_journal_home` | `work:coordinate` | no | optional | yes | Version the owner's portable Git-backed journal-home binding without transferring journal files. It is read only while the project's repository preset names no journal repository with a path; the board no longer offers an editor for it. |  |
| `project.set_repositories` | `work:coordinate` | no | optional | admin | Version the project repository preset carried in attended-worker heartbeats. | The journal-home alias must name an entry whose role is `journal`. |
| `project.plan.index` | `work:observe` | yes | yes | yes | Read each plan item's source text, summary, status, dependencies, attachment references, and derived-state hashes. |  |
| `project.plan.item` | `work:observe` | yes | yes | yes | Read one complete authoritative plan item by its canonical URI. |  |
| `project.plan.resolve` | `work:observe` | yes | yes | yes | Resolve a bounded explicit set of plan-item references and project-scoped keys in one query, naming every absent selector. |  |
| `project.plan.search` | `work:observe` | yes | yes | yes | Search plan source and summaries lexically, or with one explicitly requested and accounted query embedding. |  |
| `project.plan.import` | `work:coordinate` | no | optional | yes | Validate a complete plan package and atomically replace this project's work-item rows. | Replaces the whole plan. Rare and destructive. A person needs it on their own project Card (both presets tick it; the owner always may). |
| `project.references.preview` | `work:coordinate` | no | optional | yes | Show the exact project-wide URI rewrite without changing stored state. | A person needs it on their own project Card (both presets tick it; the owner always may). |
| `project.references.migrate` | `work:coordinate` | no | optional | yes | Apply one reviewed project-wide URI rewrite under generation and retry fences. | Review the preview first. A person needs it on their own project Card (both presets tick it; the owner always may). |
| `plan.item.create` | `work:coordinate` | optional | yes | yes | Create one authoritative plan item. | Filing work is the coordinator's job. Give it to a worker only if workers file their own findings. A person needs it on their own project Card (both presets tick it; the owner always may). |
| `work.accept` | `work:review` | no | yes | yes | Compatibility alias for review.accept. |  |
| `review.accept` | `work:review` | no | yes | yes | Accept submitted result evidence and move the item from review to done. | A worker never accepts its own work (identity rule). |
| `review.return` | `work:review` | no | yes | yes | Return work to todo for rework with a durable reason. The assignee keeps the assignment under a new ownership version and is told so. |  |
| `review.cancel` | `work:review` | no | yes | yes | Cancel work in review with a durable reason and evidence record. |  |
| `project.people.invite` | `work:admin` | no | no | yes | Invite an existing KDCube user by email and decide what their project Card holds. | For a person, being a project admin decides (an admin of this Problem Board project, not a KDCube user role; operator, 2026-09-26): a project admin invites whatever their Card holds; a member is refused `work_control_card_admin_only`. An agent may hold it only when its own Card was granted it. |
| `project.people.invitation.withdraw` | `work:admin` | no | no | yes | Close a pending invitation and revoke its pending Control Card. | For a person, the project admin role authorizes it (operator, 2026-09-26); an agent needs `project.people.invite` on its Card. Redemption closes before external revocation, which is retried when unavailable. |
| `project.people.remove` | `work:admin` | no | no | yes | Take a person off the project. Their access ends at their next request, named `work_project_person_removed`; their authored history keeps its author. | The browser action is authorized by the caller's `project.people.set_role` capability. The owner and the last admin are not removed; nobody removes themselves. After the move to Cards their Control Card is revoked (My Card fails closed with it), retried on an admin's next People view when Connection Hub is unavailable. |
| `project.people.history` | reader | no | no | yes | One page of the project's people changes, newest first: invites, withdrawals, joins, roles, Card decisions (operations added and removed), removals, ownership and the move to Cards, each with who acted, whom it concerned and when. | Read with the same authority as `project.people.list`. Keyset paging (`cursor`, `limit` up to 100). Thread privacy events are not shown. |
| `project.people.transfer_ownership` | identity rule | no | no | yes | The owner hands ownership to another admin; the former owner stays an admin. | An identity rule, not a Card operation: only the current owner. The operator mailbox and the owner-only project re-registration follow the new owner. |
| `project.people.set_role` | `work:admin` | no | no | yes | Apply the admin or member preset to a person's project Card. | A role is a preset, the Card is the truth. For a person, only a project admin applies one (operator, 2026-09-26). |
| `project.people.card.update` | `work:admin` | no | no | yes | Decide the operations one person's project Card holds. | For a person, only a project admin (of this Problem Board project), any Card including their own and the owner's (operator, 2026-09-26); a member is refused `work_control_card_admin_only`. After the move to Cards the decision is written to the Control Card at once. The creator's Card always keeps the people operations. |
| `plan.item.update` | `work:coordinate` | yes | yes | yes | Update one plan item under its current revision. | Operator ruling 2026-09-22: every agent gets it, with `plan.notes.list`. A worker that cannot correct the wording of the item it works on has to ask the coordinator to type for it. A person needs it on their own project Card (both presets tick it; the owner always may). |
| `work.status.set` | `work:coordinate` | yes | yes | yes | Set one work item's canonical status and update its assignment projection in the same transaction. | Operator ruling, 2026-09-22: status is set by whoever holds this, including the agent moving its own work to working. |
| `plan.item.delete` | `work:coordinate` | no | optional | yes | Delete one unassigned leaf item under its current revision. | A person needs it on their own project Card (both presets tick it; the owner always may). |
| `plan.note.append` | `work:coordinate` | yes | yes | yes | Append one note and advance the item revision atomically. | Notes carry findings and rulings on the item. A person needs it on their own project Card (both presets tick it; the owner always may). |
| `plan.notes.list` | `work:observe` | yes | yes | yes | Page the authoritative notes attached to one plan item. | Operator ruling 2026-09-22: every agent gets it. The notes carry the decisions on an item, and a route that points at them is useless to a worker that cannot read them. |
| `project.plan.embedding_status` | `work:observe` | no | optional | optional | Read which plan items have missing or stale embeddings without model use or writes. |  |
| `project.control.get` | `work:observe` | yes | yes | yes | Read the project's linked Connection Hub Control Card, catalog state, project properties, and participant links. |  |
| `project.control.initialize` | `work:coordinate` | no | optional | yes | Create the project's credentialless Connection Hub Card and attach it to current participants. | One-time project setup. A person creates it as a project admin, by role; see [Cards](cards.md). |
| `project.control.update` | `work:coordinate` | no | optional | yes | Update the project's version-control property on its current Control Card revision. | A person edits it as a project admin, by role; see [Cards](cards.md). Each edit is in People History with who made it. |
| `journal.view.request` | `work:observe` | yes | yes | yes | Ask a linked local relay for one expiring journal catalog page or a complete Markdown entry. |  |
| `journal.view.close` | `work:observe` | yes | yes | yes | Erase the requesting user's temporary journal snapshot. |  |
| `project.link_worker` | `work:coordinate` | no | yes | yes | Link an idle published worker to this project. | When the project has no Control Card yet, linking its first coordinator creates it, so a person must be a project admin. The Control Card attaches through the project when the caller did not create it (see [Cards](cards.md)). |
| `project.unlink_worker` | `work:coordinate` | no | yes | yes | End one worker's current project attendance. | The agent's owner unlinks it, or a project admin unlinks any agent; the project's Control Card comes off after the attendance stops (`detach_failed` is retried by unlinking again). See [Cards](cards.md). |
| `project.coordinator.get` | `work:observe` | no | yes | yes | Read who holds the coordinator role now, the home coordinator, and whether the home coordinator is available. | Workers also get it on every heartbeat as `coordinator`. |
| `project.coordinator.hand_over` | `work:admin` | no | no | yes | Hand the acting coordinator role to one attending agent, which gains the coordinator label; the home coordinator keeps its label. | A signed-in operator only (owner, operator or admin); an agent Card is refused with `work_human_operator_required`. Revision-fenced. |
| `project.coordinator.return` | `work:admin` | no | no | yes | Return the acting coordinator role to the home coordinator; labels stay. | Operator only, revision-fenced. |
| `project.coordinator.set_away` | `work:admin` | no | no | yes | Mark the home coordinator away or back. | Away reads as unavailable whatever the session reports. Operator only. |
| `project.coordinator.make` | `work:admin` | no | no | yes | Raise an attending agent's Card to the coordinator profile, then hand it the role; both receipts. | Operator only; the Card half needs the Card's grantor. `only` repeats one half. |
| `project.coordinator.make_worker` | `work:admin` | no | no | yes | Return the role to the home coordinator, then set the agent's Card to the default worker profile; both receipts. | Operator only; refused for the home coordinator's own Card. |
| `project.coordinator.note.write` | `work:coordinate` | no | yes | no | The agent holding the coordinator role writes its part of the next handover note. | The holder only (`work_coordinator_note_not_holder`); every section required. |
| `worker.rename` | `work:coordinate` | no | optional | yes | Change a worker's display alias while retaining its stable identity. |  |
| `worker.estimate` | `work:relay` | yes | optional | yes | Record or clear until when (UTC) a worker expects to finish what it is on, with a one-line note. | A worker states its own; the owner may state a worker's. The board marks it overdue once the time has passed. |
| `worker.evict` | `work:coordinate` | no | yes | yes | Suspend one worker registration without revoking its credential. | Acts on another worker, so never a worker's own right. |
| `worker.restore` | `work:coordinate` | no | yes | yes | Return one worker registration from limbo to the pool. |  |
| `worker.retire` | `work:coordinate` | no | yes | yes | Permanently close one coding-agent session while retaining its history. | Acts on another worker, so never a worker's own right. |
| `control.enqueue` | `work:coordinate` | optional | yes | yes | Queue one bounded materialize, ping, request, replan, stop, or resume command. | A command, not mail: the receiving worker must act on it. Workers normally talk through `mail.route`. Give a worker this only when it is meant to direct other workers (operator, 2026-09-22). |
| `control.discard` | `work:coordinate` | no | yes | yes | Discard selected messages and notify the worker when one was already received. |  |
| `assignment.assign` | `work:coordinate` | no | yes | yes | Advance one work item's ownership version, bind every repository the work touches (one entry each, with base commit and branch), and address the selected worker. | Also reopens done or cancelled work; a person reopens only when this operation is on their own project Card (the member and admin presets tick it; the owner always may). Assignment never changes status. The response carries `assignee_limit` (the assignee's current usage limit, empty when not reported) and, when it is limited, `assignee_limit_warning`; the work is delivered either way. |
| `assignment.return` | `work:coordinate` | no | yes | yes | Release the active assignment of stalled work with the owner's reason. The item keeps its status. | Status is kept. |
| `assignment.list` | `work:relay` | yes | yes | no | Page assignments owned by this worker in one attended project. | Needs `work:relay`, which an operator does not hold. The board shows the operator assignments another way. |
| `workspace.shared_write.publish` | `work:relay` | yes | yes | no | Publish or replace this worker's expiring shared-workspace status. | How a worker announces which shared files or runtime it is touching. |
| `workspace.shared_write.list` | `work:relay` | yes | yes | no | Read every current shared-workspace write status in the project. | Needs `work:relay`, which an operator does not hold. |
| `workspace.shared_write.clear` | `work:relay` | yes | yes | no | Remove this worker's shared-workspace write status. |  |
| `worker.publish` | `work:relay` | yes | yes | no | Publish this coding-agent session and its logical host identity. |  |
| `worker.heartbeat` | `work:relay` | yes | yes | no | Refresh worker presence and read its current project attendance. |  |
| `control.pull` | `work:relay` | yes | yes | no | Lease controls addressed to this worker. |  |
| `control.acknowledge` | `work:relay` | yes | yes | no | Acknowledge one control after local materialization. |  |
| `control.refuse` | `work:relay` | yes | yes | no | Refuse one leased control with a bounded reason. |  |
| `control.discard_complete` | `work:relay` | yes | yes | no | Report the local outcome of a message-discard request. |  |
| `control.worker_settle` | `work:relay` | yes | yes | no | Report how the coding-agent session handled delivered input. |  |
| `mail.route` | `work:relay` | yes | yes | no | Route one addressed message to another worker or the operator. | Required for any conversation. |
| `mail.reconciliation.list` | `work:observe` | no | optional | optional | Page retained mailbox reconciliation run headers for one project. |  |
| `mail.reconciliation.read` | `work:observe` | no | optional | optional | Page normalized evidence for one completed mailbox reconciliation run. |  |
| `mail.reconciliation.publish` | `work:relay` | yes | yes | no | Publish one bounded integrity-bound batch from a host-local reconciliation receipt. |  |
| `assignment.report` | `work:relay` | yes | yes | no | Append progress or a terminal result under the current ownership version. State plus source event identifies an immutable report. | Required on every assignment. |
| `plan.nodes.publish` | `work:relay` | no | optional | no | Atomically publish the supplied plan nodes as compact indexed rows without replacing omitted nodes. | Tooling that runs on a host relay. |
| `plan.index.embed` | `work:relay` | no | optional | no | Explicitly spend on accounted embeddings for plan-index rows whose source-derived search content changed. | Spends on accounted embeddings. |
| `event.publish` | `work:relay` | yes | yes | no | Publish one bounded project service event. |  |
| `journal.view.publish` | `work:relay`, `work:journal:view` | yes | yes | no | Publish one requested journal page or complete entry, including its next-page cursor. | Runs on the worker's host. |
| `journal.view.fail` | `work:relay`, `work:journal:view` | yes | yes | no | Report a bounded failure for a requested journal view. |  |
| `note.view.publish` | `work:relay` | yes | yes | no | Publish one requested page of work-item notes. | Runs on the worker's host. |
| `note.view.fail` | `work:relay` | yes | yes | no | Report a bounded failure for a requested note page. |  |
| `project.report.publish` | `work:relay` | yes | yes | no | Publish one immutable project progress report and its evidence refs. |  |
| `project.report.fail` | `work:relay` | yes | yes | no | Report a bounded failure for a requested project report. |  |
| `session.resume.publish` | `work:relay` | yes | yes | no | Publish an expiring local command for resuming this agent session. |  |
| `session.resume.fail` | `work:relay` | yes | yes | no | Report a bounded failure to prepare a session-resume command. |  |
| `attachment.request_upload` | `work:relay` | yes | yes | no | Reserve a governed upload slot for a worker-produced file. | The operator uploads through the browser, not this operation. |

The catalog also carries `review.assign` (`work:coordinate`): it names who
reviews an item in review, a linked agent or a person once the work is merged
and deployed. The coordinator routes reviews with it, and a person needs it on
their own project Card (the admin preset ticks it). See [Review](review.md).

## Keeping this page true

The operation list and grants come from the app descriptor, which mirrors
`PROBLEM_BOARD_OPERATION_POLICIES` in
`project_board.contract.worker_operation_contract` (in this package). When an
operation is added there, its row is added here with a decision for each
actor.
