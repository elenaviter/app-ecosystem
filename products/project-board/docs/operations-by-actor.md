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
  - identity rules
see_also:
  - ./README.md
  - ./operations-and-rules.md
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
| **operator** | a person who owns or operates the project | the person's Control Card for the project, held and edited in Connection Hub; the board only reads it |

The **project Control Card** is the project's ceiling. A caller Card linked to
it can use only what both allow (the AND/OR rule is on the Control Card). An
operation added to the catalog after the Control Card was saved arrives
unticked, so under AND it is withheld from every agent until a project admin
ticks it there ([Cards](cards.md#the-project-control-card-and-an-agents-card)).

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
| `work:admin` | Administer project people | Invite people, set their role and take them off the project. What a person's Card holds is edited in Connection Hub, not here (H1, app-ecosystem#199). | no, unless its owner ticks it | no, unless its owner ticks it | on an admin's project Card |

A worker needs `work:coordinate` only because a few operations it must have
sit under it. For a worker the **ticked operations** are the real limit: tick
these, and beyond them only what the operator chooses to add.

| operation | why a worker has it |
| --- | --- |
| `work.status.set` | It sets the status of the work it is doing. |
| `plan.note.append` | It writes its findings onto the item it is assigned. Granted to every agent on 2026-09-23, after a worker with an open assignment was refused it and had to route its finding through the coordinator, which made the record depend on the coordinator being awake. |
| `plan.item.update` | It corrects the wording of the item it works on. Granted to every agent on 2026-09-22, with `plan.notes.list`. |
| `plan.item.delete` | It removes an unassigned leaf item it filed by mistake. Granted to every agent on 2026-10-03 (W490). |
| `work.item.save` | It hands an item on, setting the next actor and the status in one save. Granted to every agent on 2026-10-03 (W490). |

Two more sit under `work:coordinate` and are optional for a worker, by the
operator's decision rather than by default: `plan.item.create`, when workers
file their own findings, and `control.enqueue`, when a worker directs other
workers. The operations table marks both.

This list and the **worker** column of the operations table are the same
statement written twice; when they drift, the grant-level row is the one
people read first. A narrower permission that holds these without the
rest of `work:coordinate` is an open question.

## How to read the table

- **yes**: the actor needs it for its normal role.
- **optional**: give it only when that actor is meant to take on this part.
- **admin** (operator column): a project admin needs it; the admin preset ticks
  it and the member preset does not.
- **admin (role)** (operator column): a project admin may do it by role, whatever
  their Card holds, and a member may not even with it on their Card (identity
  rule `project_admin_by_role`). It is not a Card operation for a person:
  Connection Hub does not show it on a person's Card (`person_card: false` in the
  board's catalog).
- **no**: the actor should not hold it. A worker does not hold operations that
  remove, suspend or re-route other workers. Deleting an unassigned leaf item
  is a default worker operation (W490). Directing
  other workers with `control.enqueue` is optional.
- The **permission** column is the service permission each operation declares
  in the app descriptor. On the Card screen these are the **Service
  permissions** chips. An operation works only when the Card holds both its
  service permission and the operation itself (the **Tools**).

## A person's Card: the two presets

A person's project Card is checked operation by operation. Applying a role to a
person ticks one of two presets on their Card; an admin can then tick or untick
single operations in Connection Hub. Reading the project (plan, items, notes,
people, agents, board) needs no Card operation: membership is enough (identity
rule `project_membership`).

| Preset | Operations it ticks | Service permissions |
| --- | --- | --- |
| **member** (any operator) | `review.accept`, `review.return`, `review.cancel`; `assignment.assign`, `assignment.return`, `work.status.set`, `work.assignee.set`; `plan.item.create`, `plan.item.update`, `plan.item.delete`, `plan.note.append`, `project.plan.import`, `project.references.preview`, `project.references.migrate`; `project.link_worker`, `project.unlink_worker`, `worker.evict`, `worker.restore`, `control.enqueue` | `work:review`, `work:coordinate` |
| **admin** | everything in member, plus `review.assign` | `work:review`, `work:coordinate` |

- **Opt-in:** `project.role.manage` (`work:admin`) is in neither preset; an
  admin ticks it on purpose (operator decision, W517).
- **Not on any person's Card:** a project admin, by role, invites people, sets
  their roles and removes them, decides every Card on the project and the
  project Control Card, uses the coordinator levers, and sets the project's
  configuration (repositories, journal home, commit identity, files): identity
  rule `project_admin_by_role`. These operations carry `person_card: false` in
  the board's catalog, so Connection Hub does not list them on a person's Card;
  `work:admin` is needed only on an agent's Card. The owner may do every Card
  operation whatever their Card holds (`owner_exempt_from_card`).

## Rules no Card changes

Almost every refusal is about the Card: the caller's Card lacks the operation
(`work_worker_operation_not_granted`, or `work_worker_operation_withheld_by_control_card`
when the project's Control Card withholds it), and the Card's owner can add it.
A few decisions are **identity rules**: rules about who someone is. No Card
holds them and no consent changes them, and a refusal from one says so, so
nobody waits for a permission that cannot be granted. Every board decision is
either an operation on the caller's Card, with that operation's business
rules, or one of these rules (operator ruling, 2026-09-22).

The board states them once, in `services/identity_rules.py`, with the ids
below; a gate that applies one names it (in its docstring, and as
`identity_rule` in a refusal's details). This table is the same list. What decides
each operation for a person, and each operation's business rules, is the
generated page [Operations and rules](operations-and-rules.md).

| Rule id | Rule | Refusal | Who can act instead |
| --- | --- | --- | --- |
| `project_membership` | A person on the project reads it: the plan, items, notes, reports, people, workers, the board and the timeline. Membership scopes a person to a project; it is not an operation on a Card (operator, 2026-09-27). | `work_project_role_required`, or `work_project_person_removed` for a removed person | A project admin invites the person. |
| `operator_inbox_people_only` | The operator inbox is for people: threads, replies, read state and the worker directory, for a signed-in person on the project (not an older read-only viewer), never an agent (operator, 2026-09-27). | `work_human_operator_required` | A person on the project. |
| `person_views_people_only` | The views the board opens on a person's screen (a project report, a note view, a session-resume view, a command to the local plan host) are requested and managed by a signed-in person on the project; agents publish them (operator, 2026-09-27). | `work_human_operator_required` | A person on the project. |
| `private_thread` | A thread its person made private is visible only to that person, the timeline included. | The thread is not listed. | The person who made it private. |
| `project_admin_by_role` | A project admin by role (owner or admin in this project) decides every Card on the project in Connection Hub, the project Control Card, the coordinator levers, the people on the project and its configuration (repositories, journal home, commit identity), however narrow their own Card, until the admin role itself is removed. | `work_control_card_admin_only`, `work_project_admin_required`; only a person moves the coordinator role (`work_human_operator_required`) | A project admin, signed in. |
| `project_owner_is_a_person` | A project is registered by, and owned by, a signed-in person. | `work_project_owner_is_a_person` | A person registers it. |
| `owner_exempt_from_card` | The project's owner may do every Card operation on their project, whatever their Card holds, so a project always has someone who can act. | None: the owner is never refused `work_card_operation_required`. | |
| `last_admin` | The last admin of a project can be neither removed nor demoted, and the owner is not removed; ownership moves only by transfer to another admin, by the current owner. | `work_project_last_admin`, `work_project_owner_not_removable`, `work_project_owner_role_fixed` | The current owner transfers ownership first. |
| `own_role` | A person does not change their own project role. | Named as the gates move (W260). | Another project admin. |
| `worker_pool_is_its_grantors` | An agent belongs to the person who approved it. Someone else's agent is linked, unlinked or managed only as that person allows, through a share or a project it is linked to. | `work_worker_not_owned` ("a rule, not a missing permission") | The agent's owner, or a project admin the owner shared it with. |
| `shared_write_agents_only` | Shared workspace writes are made by agents, each as itself. | Named as the gates move (W260). | An agent. |
| `role_holder_decides_handovers` | The agent holding an optional project role, such as the knowledge keeper, decides the hand-overs mailed to that role; holding the role is a condition, never authority by itself. | `project.role.handover.decide` (W517). | The agent holding the role, with the operation on its Card. |
| `coordinator_publishes_reports` | A project report is published, or failed, by the project's coordinator. | Named as the gates move (W260). | The acting coordinator. |
| `attachment_upload_people_only` | Attachments are staged by a signed-in person. | Named as the gates move (W260). | A person on the project. |

Some operations carry **business rules of their own**, which hold whatever the
Card says: no one accepts their own work (`work_review_self_forbidden`), and an
agent decides a review only as the item's named reviewer or the coordinator
(`work_review_not_reviewer`). They are listed with the operation in
[Review](review.md).

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

An agent's Card is also capped by its project's **Control Card** (AND, the default access rule; see [Cards](cards.md#the-project-control-card-and-an-agents-card)): an
operation the project Control Card does not include is refused with
`work_worker_operation_withheld_by_control_card`, which names that Control
Card, whatever the agent's own Card holds. Re-consent does not help. A project
admin adds the operation to the project Control Card in Connection Hub (reached
from Manage project access), then the
agent's Card is refreshed (Refresh coordinator Card, Make coordinator or Make
worker). On 2026-09-26 new coordinator operations reached the catalog, and
`project.coordinator.note.write` stayed refused after Refresh until the operator
ticked it on the project Control Card; the refusal then still read "written
at consent", which sent the team to the catalog.

| operation | permission | worker | coordinator | operator | what it does | why |
| --- | --- | --- | --- | --- | --- | --- |
| `project.register` | `work:coordinate` | no | optional | yes | Register a project under the signed-in owner. | An owner decision. A coordinator may do it on the owner's instruction. |
| `project.set_journal_home` | `work:coordinate` | no | optional | admin (role) | Version the owner's portable Git-backed journal-home binding without transferring journal files. It is read only while the project's repository preset names no journal repository with a path; the board no longer offers an editor for it. |  |
| `project.set_repositories` | `work:coordinate` | no | optional | admin (role) | Version the project repository preset carried in attended-worker heartbeats. | The journal-home alias must name an entry whose role is `journal`. |
| `project.set_commit_identity` | `work:coordinate` | no | optional | admin (role) | Set the email every agent of the project commits with, as `<agent alias> <email>`; part of the repository preset and advances its revision. | One email address, or empty to clear; `person_card: false`. |
| `project.set_files` | `work:coordinate` | no | yes | admin (role) | Set where the project's files live: repository and path, a purpose (instructions, facts, environment) or a one-line description. | The board keeps the list, never the content; `person_card: false`. |
| `project.files.edit` | `work:coordinate` | no | yes | no | Check, before an edit, that the agent may edit the project's files in its repositories. | The edit is a commit the board cannot see; without it the agent proposes the change to the coordinator. |
| `project.file.edit.result` | `work:relay`, `work:journal:view` | yes | yes | no | Report how a project-file edit made on the board landed: committed, pull request opened, branch pushed, unchanged, or refused. | The relay that made the edit reports it. |
| `project.github.use` | `work:relay` | yes | yes | no | Check that the agent may use GitHub on the project's repositories, and list the card's GitHub repositories. | The token comes from Connection Hub under the owner's GitHub link; Connection Hub asks the board again for the exact repository, which must be on the project card, and the agent must attend the project. |
| `project.plan.index` | `work:observe` | yes | yes | yes | Read each plan item's source text, summary, status, dependencies, attachment references, and derived-state hashes. |  |
| `project.plan.item` | `work:observe` | yes | yes | yes | Read one complete authoritative plan item by its canonical URI. |  |
| `project.plan.resolve` | `work:observe` | yes | yes | yes | Resolve a bounded explicit set of plan-item references and project-scoped keys in one query, naming every absent selector. |  |
| `project.plan.search` | `work:observe` | yes | yes | yes | Search plan source and summaries lexically, or with one explicitly requested and accounted query embedding. |  |
| `project.plan.history` | `work:observe` | yes | yes | yes | Read one item's recorded actions, newest first, as a bounded page pinned to one generation. Each `item.*` row names who made the change, or says the writer is unknown (W538). | Reading the plan; a person needs only project membership. |
| `project.plan.import` | `work:coordinate` | no | optional | yes | Validate a complete plan package and atomically replace this project's work-item rows. | Replaces the whole plan. Rare and destructive. A person needs it on their own project Card (both presets tick it; the owner always may). |
| `project.references.preview` | `work:coordinate` | no | optional | yes | Show the exact project-wide URI rewrite without changing stored state. | A person needs it on their own project Card (both presets tick it; the owner always may). |
| `project.references.migrate` | `work:coordinate` | no | optional | yes | Apply one reviewed project-wide URI rewrite under generation and retry fences. | Review the preview first. A person needs it on their own project Card (both presets tick it; the owner always may). |
| `plan.item.create` | `work:coordinate` | optional | yes | yes | Create one authoritative plan item; the board assigns its next free W number. | Filing work is the coordinator's job. Give it to a worker only if workers file their own findings. A person needs it on their own project Card (both presets tick it; the owner always may). |
| `work.accept` | `work:review` | no | yes | yes | Compatibility alias for review.accept. |  |
| `review.accept` | `work:review` | no | yes | yes | Accept submitted result evidence and move the item from review to done. | A worker never accepts its own work (identity rule). |
| `review.return` | `work:review` | no | yes | yes | Return work to working for rework with a durable reason. The assignee keeps the assignment under a new ownership version and is told so. |  |
| `review.cancel` | `work:review` | no | yes | yes | Cancel work in review with a durable reason and evidence record. |  |
| `review.assign` | `work:coordinate` | yes | yes | admin | Name who reviews an item in review: a linked agent other than the one who did the work, or `operator` (a project admin) once the work is merged and deployed (W326). The reviewer becomes the item's assignee. | An author arranges its own reviewer (collaboration Rule 16); the coordinator routes reviews. A person needs it on their own project Card (the admin preset ticks it). See [Review](review.md). |
| `project.people.invite` | `work:admin` | no | no | admin (role) | Invite an existing KDCube user by email, as a member or a project admin. The role's preselection fills their Control Card once, when it is created; an invitation that sends a list of operations is refused `work_invitation_operations_in_connection_hub`. | For a person, being a project admin decides (an admin of this Problem Board project, not a KDCube user role; operator, 2026-09-26): a project admin invites whatever their Card holds; a member is refused `work_control_card_admin_only`. An agent may hold it only when its own Card was granted it. |
| `project.people.invitation.withdraw` | `work:admin` | no | no | admin (role) | Close a pending invitation and revoke its pending Control Card. | For a person, the project admin role authorizes it (operator, 2026-09-26); an agent needs `project.people.invite` on its Card. Redemption closes before external revocation, which is retried when unavailable. |
| `project.people.remove` | `work:admin` | no | no | admin (role) | Take a person off the project. Their access ends at their next request, named `work_project_person_removed`; their authored history keeps its author. | The browser action is authorized by the caller's `project.people.set_role` capability. The owner and the last admin are not removed; nobody removes themselves. After the move to Cards their Control Card is revoked (My Card fails closed with it), retried on an admin's next People view when Connection Hub is unavailable. |
| `project.people.history` | reader | no | no | yes | One page of the project's people changes, newest first: invites, withdrawals, joins, roles, Card decisions (operations added and removed), removals, ownership and the move to Cards, each with who acted, whom it concerned and when. | Read with the same authority as `project.people.list`. Keyset paging (`cursor`, `limit` up to 100). Thread privacy events are not shown. |
| `project.people.transfer_ownership` | identity rule | no | no | yes | The owner hands ownership to another admin; the former owner stays an admin. | An identity rule, not a Card operation: only the current owner. The operator mailbox and the owner-only project re-registration follow the new owner. |
| `project.people.set_role` | `work:admin` | no | no | admin (role) | Make a person a project admin, or a member again. | The role decides only whether they are a project admin; it writes no Card. For a person, only a project admin sets it (operator, 2026-09-26), and a project admin makes themselves a member only while another admin remains (`work_project_people_sole_admin`). |
| `project.people.card.update` | `work:admin` (retired) | no | no | no | Retired by H1: a person's Card is edited in Connection Hub. It answers everyone `work_control_card_edit_in_connection_hub` (410) and writes nothing; it stays in the catalog so an older client gets that answer instead of an unknown operation. | A project admin opens the person's Control Card from Team > People and edits it in Connection Hub, which asks the board only whether they are a project admin ([Cards](cards.md#the-rules)). |
| `plan.item.update` | `work:coordinate` | yes | yes | yes | Update one plan item under its current revision. | Operator ruling 2026-09-22: every agent gets it, with `plan.notes.list`. A worker that cannot correct the wording of the item it works on has to ask the coordinator to type for it. A person needs it on their own project Card (both presets tick it; the owner always may). |
| `work.status.set` | `work:coordinate` | yes | yes | yes | Set one work item's canonical status without changing its assignee. | Operator rulings, 2026-09-22 and 2026-09-29: status is set by whoever holds this, including the agent moving its own work to working; `item.assignee` remains the assignee for every status. |
| `work.assignee.set` | `work:coordinate` | yes | yes | yes | Set or clear who does the next work on an item, in any status. The status stays; the new owner uses its existing Card and repository scope, and the previous owner is fenced. | A worker hands its item on itself (collaboration Rule 16). A person needs it on their own project Card (both presets tick it; the owner always may). |
| `work.item.save` | `work:coordinate` | yes | yes | yes | Save an item's status, assignee or both in one transaction under its current revision; each supplied field needs its own operation. | Operator ruling 2026-10-03 (W490): a default worker operation, so a hand-off moves status and assignee together. |
| `plan.item.delete` | `work:coordinate` | yes | yes | yes | Delete one unassigned leaf item under its current revision. | Operator ruling 2026-10-03 (W490): a default worker operation. The board still refuses an assigned or non-leaf item. |
| `plan.note.append` | `work:coordinate` | yes | yes | yes | Append one note and advance the item revision atomically. | Notes carry findings and rulings on the item. A person needs it on their own project Card (both presets tick it; the owner always may). |
| `plan.notes.list` | `work:observe` | yes | yes | yes | Page the authoritative notes attached to one plan item. | Operator ruling 2026-09-22: every agent gets it. The notes carry the decisions on an item, and a route that points at them is useless to a worker that cannot read them. |
| `work.attachment.link` | `work:observe` | yes | yes | yes | Issue one signed download link for one file attached to one item, bound to the caller (W485). | An item read names its files without links; this is how one file is fetched. |
| `project.plan.embedding_status` | `work:observe` | no | optional | optional | Read which plan items have missing or stale embeddings without model use or writes. |  |
| `project.control.get` | `work:observe` | yes | yes | yes | Read the project's linked Connection Hub Control Card, catalog state, project properties, and participant links. |  |
| `project.control.initialize` | `work:coordinate` | no | optional | admin (role) | Create the project's credentialless Connection Hub Card and attach it to current participants. | One-time project setup. A person creates it as a project admin, by role; see [Cards](cards.md). |
| `project.control.update` | `work:coordinate` | no | optional | admin (role) | Change the project Control Card's access rule (`composition_mode`: `and` or `or`) on its current revision (`expected_revision`). An optional `properties` object is still validated but nothing reads it, and the board no longer shows it. | A person edits it as a project admin, by role; see [Cards](cards.md). Each edit is in People History with who made it. |
| `journal.view.request` | `work:observe` | yes | yes | yes | Ask a linked local relay for one expiring journal catalog page or a complete Markdown entry. |  |
| `journal.view.close` | `work:observe` | yes | yes | yes | Erase the requesting user's temporary journal snapshot. |  |
| `project.link_worker` | `work:coordinate` | no | yes | yes | Link an idle published worker to this project. | When the project has no Control Card yet, linking its first coordinator creates it, so a person must be a project admin. The Control Card attaches through the project when the caller did not create it (see [Cards](cards.md)). |
| `project.unlink_worker` | `work:coordinate` | no | yes | yes | End one worker's current project attendance. | The agent's owner unlinks it, or a project admin unlinks any agent; the project's Control Card comes off after the attendance stops (`detach_failed` is retried by unlinking again). See [Cards](cards.md). |
| `project.coordinator.get` | `work:observe` | no | yes | yes | Read who holds the coordinator role now, the home coordinator, and whether the home coordinator is available. | Workers also get it on every heartbeat as `coordinator`. |
| `project.coordinator.hand_over` | `work:admin` | no | no | admin (role) | Hand the acting coordinator role to one attending agent, which gains the coordinator label; the home coordinator keeps its label. | A signed-in project admin only (owner included); an agent Card is refused with `work_human_operator_required`. Revision-fenced. |
| `project.coordinator.return` | `work:admin` | no | no | admin (role) | Return the acting coordinator role to the home coordinator; labels stay. | A project admin only (owner included), revision-fenced. |
| `project.coordinator.set_away` | `work:admin` | no | no | admin (role) | Mark the home coordinator away or back. | Away reads as unavailable whatever the session reports. A project admin only (owner included). |
| `project.coordinator.make` | `work:admin` | no | no | admin (role) | Raise an attending agent's Card to the coordinator profile, then hand it the role; both receipts. | A project admin only (owner included); the Card half needs the Card's grantor. `only` repeats one half. |
| `project.coordinator.make_worker` | `work:admin` | no | no | admin (role) | Return the role to the home coordinator, then set the agent's Card to the default worker profile; both receipts. | A project admin only (owner included); refused for the home coordinator's own Card. |
| `project.coordinator.note.write` | `work:coordinate` | no | yes | no | The agent holding the coordinator role writes its part of the next handover note. | The holder only (`work_coordinator_note_not_holder`); every section required. |
| `project.announcement.publish` | `work:coordinate` | yes | yes | no | Publish the project's current status, progress, blocker or notice, or a deployment window's opening, delay or all-clear. The newest replaces the one before on the board. | The board accepts it only from the agent holding the coordinator role (W412): the worker profile carries it so that the role, not the Card, decides. |
| `project.role.get` | `work:observe` | optional | yes | no | Read an optional role the board carries, such as the knowledge keeper: its state, its holder and whether the holder is available. | Not in the default worker profile; give it to an agent that hands work over to a role. A person sees roles on the board through project membership. |
| `project.role.manage` | `work:admin` | no | no | optional | Declare or undeclare an optional project role, or name or clear the agent holding it, under the role's revision. | A person only, and opt-in: no preset ticks it, and no Card gains it at creation, Refresh or migration; an admin ticks it on purpose (operator decision, W517). |
| `project.role.declare` | `work:admin` | no | no | optional | Alias of `project.role.manage` that declares or undeclares the role. | As `project.role.manage`. |
| `project.role.assign` | `work:admin` | no | no | optional | Alias of `project.role.manage` that names or clears the agent holding the role. | As `project.role.manage`. |
| `project.role.handover.decide` | `work:coordinate` | optional | optional | no | The agent holding an optional role records a hand-over to that role as incorporated (with its result), declined, or needing evidence (with a reason). Settling the mail is not this. | Only for the agent that holds the role; holding it is a condition, never authority by itself (identity rule `role_holder_decides_handovers`). |
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
| `workspace.shared_write.list` | `work:relay` | yes | yes | yes (list only) | Read every current shared-workspace write status in the project. | An agent needs `work:relay` on its Card and must attend the project. Any signed-in person on the project, members included, sees the same list on the board; only an agent publishes or clears its own entry. |
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

Four rows above are not Card operations: `project.people.invitation.withdraw`,
`project.people.remove`, `project.people.history` and
`project.people.transfer_ownership` are decided by the project admin role or an
identity rule, so the catalog does not carry them.

## Keeping this page true

The operation list and grants come from the app descriptor, which mirrors
`PROBLEM_BOARD_OPERATION_POLICIES` in
`project_board.contract.worker_operation_contract` (in this package). When an
operation is added there, its row is added here with a decision for each
actor. `tests/test_operations_by_actor_page.py` fails when a catalog operation
has no row here, or a row names a permission the catalog does not grant.
