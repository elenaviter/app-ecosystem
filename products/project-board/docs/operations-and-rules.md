---
id: project-board-operations-and-rules
title: Operations And Rules
summary: For every Problem Board operation, what decides it for a person (their Card, membership or a stated identity rule) and its business rules, generated from the board's own tables.
tags:
  - project-board
  - authorization
  - operations
  - cards
keywords:
  - identity rules
  - business rules
  - project Card
  - membership
  - generated
see_also:
  - ./operations-by-actor.md
  - ./cards.md
---

# Operations And Rules

Generated from the Problem Board app: the delegated catalog, `services/operation_rules.py`
and `services/identity_rules.py`. Do not edit this page; change those sources and run
`scripts/generate_operation_rules_page.py`. A test fails when the two differ.

Project mutations require the applicable role AND current effective Card authority.
For a person, resolve project Control -> person Control -> My Card using the
conjunction defined by each upstream Control (AND/OR). Owner and admin roles bypass neither requirement.
Do not turn OR into a blanket requirement that every raw personal tick be present in both Cards.
The required administrative Control minimum remains a separate predicate.
Agent channels keep their separately stated Card contracts, including explicit raw-Card checks.
Identity, business, ownership and revision rules are additional vetoes.
Membership-scoped reads and global lifecycle or bootstrap boundaries keep their
separately stated contracts; no ordinary mutation gains authority from a missing Card.
Which actor should hold each operation is in [Operations By Actor](operations-by-actor.md).

## Identity rules

| Rule id | Rule |
| --- | --- |
| `own_card_is_its_persons` | A person's own My Card is managed only by that person: the Reset on their own row in Team resets their own Card, never another person's, and an agent never does it. |
| `role_holder_decides_handovers` | The agent holding an optional project role, such as the knowledge keeper, decides the hand-overs mailed to that role; a person never does, and holding the role is a condition on the operation, never authority by itself. |
| `project_membership` | A person on the project reads it: the plan, items, notes, reports, people, workers, the board and the timeline. Membership scopes a person to a project; it is not an operation on a Card. |
| `operator_inbox_people_only` | The operator inbox is for people: its threads, replies, read state and worker directory are read and written by a signed-in person on the project (not an older read-only viewer), never by an agent. |
| `private_thread` | A thread its person made private is visible only to that person, the timeline included. |
| `person_views_people_only` | The views the board opens on a person's screen (a project report, a note view, a session-resume view, a command to the local plan host) are requested and managed by a signed-in person on the project; agents publish them. |
| `project_admin_by_role` | An owner or admin role is the minimum for project administration. The applicable operation must also pass the current effective Card hierarchy using each upstream Control's conjunction; the role is not a bypass. Connection Hub remains the only Card editor. |
| `project_owner_is_a_person` | A project is registered by, and owned by, a signed-in person. |
| `owner_exempt_from_card` | Retired historical exception: the owner has no ordinary project-mutation bypass. Their role AND current effective Card hierarchy must permit the applicable operation. Bootstrap and recovery do not silently grant ordinary mutation authority. |
| `last_admin` | The last admin of a project can be neither removed nor demoted, and the owner is not removed; ownership moves only by transfer to another admin. |
| `own_role` | A person does not change their own project role. |
| `worker_pool_is_its_grantors` | A worker belongs to the pool of the person who authorized it; another person reaches it only through a share or a project it is linked to. |
| `shared_write_agents_only` | Shared workspace writes are made by agents, each as itself. |
| `coordinator_publishes_reports` | A project report is published, or failed, by the project's coordinator. |
| `attachment_upload_people_only` | Attachments are staged by a signed-in person. |
| `project_file_edits_people_only` | A listed project-file edit on the board is requested by a signed-in person on the project; the coordinator writes the file through its own relay. The person still needs the applicable role AND current effective Card hierarchy for each requested operation. |

## Operations a Card holds

| Operation | Group | Permission | For a person | Business rules |
| --- | --- | --- | --- | --- |
| `project.set_goal` | project | `work:coordinate` | Role AND current effective Card hierarchy | An owner/admin role AND current effective Card hierarchy; empty clears the goal. |
| `project.cards.manage` | people | `work:admin` | Role AND current effective Card hierarchy | A person with the owner/admin role AND current effective Card hierarchy. No person's role preset ticks it. Checks authority for Connection Hub management, never writes a Card in Problem Board. Retired project.people.card.update remains retired. |
| `project.register` | project | `work:coordinate` | Identity rule `project_owner_is_a_person` | The signed-in person who registers a project is its owner and first admin. A new project is retained if Card initialization fails: work_project_cards_initialization_unavailable (503) names the incomplete step and unconfirmed cutover. This failure is not retryable by repeating registration; recovery requires explicit Card-initialization authority. No rollback, automatic reseed or legacy mutation authority is implied. Re-registering an existing project does not initialize its Cards. |
| `project.set_journal_home` | project | `work:coordinate` | Role AND current effective Card hierarchy | Read only while the repository preset names no journal repository with a path. |
| `project.set_repositories` | project | `work:coordinate` | Role AND current effective Card hierarchy | Written under the preset's current revision. Once the preset is the journal home, a repository keeps the journal role and a path. |
| `project.set_commit_identity` | project | `work:coordinate` | Role AND current effective Card hierarchy | One email address, or empty to clear, under the preset's current revision, which it advances. |
| `project.set_files` | project | `work:coordinate` | Role AND current effective Card hierarchy | An ordered list of repository alias and path, each alias one of the project's repositories; the instructions, facts and environment purposes at most once each; written under the list's current revision. |
| `project.files.edit` | project | `work:coordinate` | Role AND current effective Card hierarchy | A permission check before editing the project's files in its repositories; the coordinator profile holds it. |
| `project.github.use` | project | `work:relay` | An agent's channel; not a person's | An attending worker/coordinator AND project.github.use on both its raw live Card and the exact project's Control Card. Connection Hub's signed peer question binds the current agent, owner, project and listed repository before any My Card key or token is read; nonce/replay/expiry fences remain. No owner/admin, relay, snapshot or Effective Card OR bypass. |
| `operation.receipt.get` | agent | `work:relay` | An agent's channel; not a person's | W575: an agent reads the outcome of its own earlier request after a lost reply; the board authorizes the stored original operation for the caller, and the request only selects the caller's own row. no_record is never proof of no effect. Recovery resends no mutation automatically. |
| `project.plan.index` | plan | `work:observe` | Membership (`project_membership`) |  |
| `project.plan.item` | plan | `work:observe` | Membership (`project_membership`) |  |
| `work.attachment.link` | plan | `work:observe` | Membership (`project_membership`) | W485: one signed link to one file on one item, bound to the caller; reads never carry links. |
| `project.plan.history` | plan | `work:observe` | Membership (`project_membership`) |  |
| `project.plan.resolve` | plan | `work:observe` | Membership (`project_membership`) |  |
| `project.plan.search` | plan | `work:observe` | Membership (`project_membership`) |  |
| `project.plan.import` | plan | `work:coordinate` | Role AND current effective Card hierarchy | Replaces the whole plan from a complete package. The applicable role AND current effective Card hierarchy are required, using each upstream Control's conjunction; owner and admin roles bypass neither requirement. |
| `project.references.preview` | plan | `work:coordinate` | Role AND current effective Card hierarchy | Stores a complete integrity-bound mapping artifact, without changing project references. The applicable role AND current effective Card hierarchy are required, using each upstream Control's conjunction; owner and admin roles bypass neither requirement. |
| `project.references.migrate` | plan | `work:coordinate` | Role AND current effective Card hierarchy | Applies exactly the rewrite a preview returned. The applicable role AND current effective Card hierarchy are required, using each upstream Control's conjunction; owner and admin roles bypass neither requirement. |
| `project.plan.embedding_status` | plan | `work:observe` | Membership (`project_membership`) |  |
| `plan.item.create` | plan | `work:coordinate` | Role AND current effective Card hierarchy | The applicable role AND current effective Card hierarchy are required, using each upstream Control's conjunction; owner and admin roles bypass neither requirement. |
| `work.status.set` | work | `work:coordinate` | Role AND current effective Card hierarchy | Entering review needs review.look_at and review.could_not_verify. The displayed assignee and ownership version stay unchanged, including an empty assignee in Working; Done and Cancelled close execution without review approval, and a nonterminal status alone does not reopen it. An ordinary status edit, including leaving Review, records no review verdict; dedicated review operations retain their own authority and audit fences without additional field grants. The applicable role AND current effective Card hierarchy are required, using each upstream Control's conjunction; owner and admin roles bypass neither requirement. |
| `work.item.save` | work | `work:coordinate` | Membership (`project_membership`) | People and agents supply status, assignee, or both; omitted fields stay unchanged and both supplied fields commit or neither does. There is no composite Card grant: each supplied field needs only work.status.set or work.assignee.set; plan.item.update is checked separately for supplied labels. |
| `work.assignee.set` | work | `work:coordinate` | Role AND current effective Card hierarchy | Sets or clears the current assignee in any valid status, without changing status; every changed assignee advances the ownership fence exactly once. At a nonterminal status the new assignee does the next work using its existing Card and repository scope; Done and Cancelled keep the display owner but create no active dispatch, and selection creates no permissions or credentials. Historical contributors and stale-owner fences remain intact; actual review decisions retain their authority and no-self-review fences. The applicable role AND current effective Card hierarchy are required, using each upstream Control's conjunction; owner and admin roles bypass neither requirement. |
| `work.accept` | work | `work:review` | Role AND current effective Card hierarchy | An alias of review.accept, with its rules. |
| `review.accept` | review | `work:review` | Role AND current effective Card hierarchy | No one accepts their own work (work_review_self_forbidden). An agent decides a review only as the item's named reviewer or the acting coordinator (work_review_not_reviewer). |
| `review.return` | review | `work:review` | Role AND current effective Card hierarchy | A reason is required. An agent decides a review only as the item's named reviewer or the acting coordinator (work_review_not_reviewer). |
| `review.cancel` | review | `work:review` | Role AND current effective Card hierarchy | A reason is required. An agent decides a review only as the item's named reviewer or the acting coordinator (work_review_not_reviewer). |
| `review.assign` | review | `work:coordinate` | Role AND current effective Card hierarchy | Routes only an item in review. The applicable role AND current effective Card hierarchy are required, using each upstream Control's conjunction; owner and admin roles bypass neither requirement. |
| `project.people.invite` | people | `work:admin` | Role AND current effective Card hierarchy | An existing KDCube user, by email, with a role; their Control Card starts with that role's preselection. |
| `project.people.set_role` | people | `work:admin` | Role AND current effective Card hierarchy | A role writes no Card. A person does not change their own role while no other admin remains (last_admin, own_role). |
| `project.people.card.update` | people | `work:admin` | Retired | Answers work_control_card_edit_in_connection_hub: Cards are edited in Connection Hub. |
| `project.coordinator.get` | coordinator | `work:observe` | Membership (`project_membership`) | Agents also receive it on every heartbeat. |
| `project.role.get` | roles | `work:observe` | Membership (`project_membership`) | W517: an optional role the board carries (knowledge-keeper) and its holder. |
| `project.role.manage` | roles | `work:admin` | Role AND current effective Card hierarchy | W517: a person who is an owner or admin only (work_human_operator_required, work_project_admin_required); revision-fenced (work_role_holder_revision_conflict). The holder is an attending, active agent named by its stable worker name (work_role_holder_not_attending, work_role_holder_not_active). Opt-in: no preset ticks it. The role grants no permission. |
| `project.role.handover.decide` | roles | `work:coordinate` | An agent's channel; not a person's | W517: the agent holding the role only (work_role_handover_agent_only, work_role_handover_not_holder), under the hand-over's revision (work_role_handover_revision_conflict). Incorporated names its result_ref; declined and needs_evidence give a reason. Settling the mail is not a decision. Opt-in: no default worker profile holds it. |
| `project.role.declare` | roles | `work:admin` | Role AND current effective Card hierarchy | An alias of project.role.manage that declares or undeclares the role. |
| `project.role.assign` | roles | `work:admin` | Role AND current effective Card hierarchy | An alias of project.role.manage that names or clears the holder. |
| `project.coordinator.hand_over` | coordinator | `work:admin` | Role AND current effective Card hierarchy | A signed-in person only; revision-fenced. |
| `project.coordinator.return` | coordinator | `work:admin` | Role AND current effective Card hierarchy | A signed-in person only; revision-fenced. |
| `project.coordinator.set_away` | coordinator | `work:admin` | Role AND current effective Card hierarchy | Away reads as unavailable whatever the session reports. |
| `project.coordinator.note.write` | coordinator | `work:coordinate` | An agent's channel; not a person's | The coordinator holder only (work_coordinator_note_not_holder); every section is required. |
| `project.announcement.publish` | coordinator | `work:coordinate` | Role AND current effective Card hierarchy | An agent must hold this project's coordinator role (work_announcement_not_coordinator), and a person must be owner or admin; both need current Card authority. The newest announcement replaces the one before and stops showing when it expires. |
| `project.coordinator.make` | coordinator | `work:admin` | Role AND current effective Card hierarchy | A signed-in person only. |
| `project.coordinator.make_worker` | coordinator | `work:admin` | Role AND current effective Card hierarchy | Refused for the home coordinator's own Card. |
| `plan.item.update` | plan | `work:coordinate` | Role AND current effective Card hierarchy | Under the item's current revision; new attachment refs must be staged uploads. The applicable role AND current effective Card hierarchy are required, using each upstream Control's conjunction; owner and admin roles bypass neither requirement. |
| `plan.item.delete` | plan | `work:coordinate` | Role AND current effective Card hierarchy | Only an unassigned leaf item. The applicable role AND current effective Card hierarchy are required, using each upstream Control's conjunction; owner and admin roles bypass neither requirement. |
| `plan.note.append` | plan | `work:coordinate` | Role AND current effective Card hierarchy | The applicable role AND current effective Card hierarchy are required, using each upstream Control's conjunction; owner and admin roles bypass neither requirement. |
| `plan.notes.list` | plan | `work:observe` | Membership (`project_membership`) |  |
| `project.control.get` | control | `work:observe` | Membership (`project_membership`) |  |
| `project.control.initialize` | control | `work:coordinate` | Role AND current effective Card hierarchy | Once per project. |
| `project.control.update` | control | `work:coordinate` | Role AND current effective Card hierarchy | Each edit is in People History with who made it. |
| `journal.view.request` | reports | `work:observe` | Role AND current effective Card hierarchy | The project must have a journal home; the queued agent command also requires control.enqueue. |
| `journal.view.close` | reports | `work:observe` | Role AND current effective Card hierarchy | Erases the requesting person's own snapshot. |
| `project.link_worker` | project | `work:coordinate` | Role AND current effective Card hierarchy | Whose agent it is: its owner's, or shared with the caller (worker_pool_is_its_grantors). Linking the project's first coordinator creates its Control Card, which only a project admin does. The applicable role AND current effective Card hierarchy are required, using each upstream Control's conjunction; owner and admin roles bypass neither requirement. |
| `project.unlink_worker` | project | `work:coordinate` | Role AND current effective Card hierarchy | The agent's owner unlinks it, or a project admin unlinks any agent. The project's Control Card comes off after the attendance stops. The applicable role AND current effective Card hierarchy are required, using each upstream Control's conjunction; owner and admin roles bypass neither requirement. |
| `worker.rename` | agent | `work:coordinate` | Identity rule `worker_pool_is_its_grantors` | An agent renames only itself; a person only an agent it owns. |
| `worker.estimate` | agent | `work:relay` | Identity rule `worker_pool_is_its_grantors` | An agent states its own; its owner may state it. |
| `worker.evict` | agent | `work:coordinate` | Role AND current effective Card hierarchy | Only an agent the caller owns (worker_pool_is_its_grantors), linked to the project. The applicable role AND current effective Card hierarchy are required, using each upstream Control's conjunction; owner and admin roles bypass neither requirement. |
| `worker.restore` | agent | `work:coordinate` | Role AND current effective Card hierarchy | Only an agent the caller owns (worker_pool_is_its_grantors), linked to the project. The applicable role AND current effective Card hierarchy are required, using each upstream Control's conjunction; owner and admin roles bypass neither requirement. |
| `worker.retire` | agent | `work:coordinate` | Identity rule `worker_pool_is_its_grantors` | Permanent; the retirement names the agent to confirm it, and revokes the Card the session was enrolled with (a failed revocation is recorded and retried by retiring again). |
| `control.enqueue` | agent | `work:coordinate` | Role AND current effective Card hierarchy | Only to an agent linked to the project, the caller's own or shared with them. Attachments are staged by a person (attachment_upload_people_only). The applicable role AND current effective Card hierarchy are required, using each upstream Control's conjunction; owner and admin roles bypass neither requirement. |
| `control.discard` | agent | `work:coordinate` | Role AND current effective Card hierarchy | Each project target requires control.discard through its current effective Card hierarchy; only messages the caller sent may be discarded (work_control_discard_target_denied). No-project lifecycle targets keep their separately scoped ownership contract; no project authority is fabricated. |
| `assignment.assign` | work | `work:coordinate` | Role AND current effective Card hierarchy | Explicit authorized reopen establishes a new ownership period at review, done or cancelled without changing status or started_at; validated Board evidence makes its notice work to begin, and its new owner's first working report needs no preliminary status save. The applicable role AND current effective Card hierarchy are required, using each upstream Control's conjunction; owner and admin roles bypass neither requirement. |
| `assignment.return` | work | `work:coordinate` | Role AND current effective Card hierarchy | The owner's reason is required; the item keeps its status. The applicable role AND current effective Card hierarchy are required, using each upstream Control's conjunction; owner and admin roles bypass neither requirement. |
| `assignment.list` | work | `work:relay` | Membership (`project_membership`) | An agent pages its own assignments; the active coordinator holder may page any linked worker. |
| `workspace.shared_write.publish` | agent | `work:relay` | Identity rule `shared_write_agents_only` |  |
| `workspace.shared_write.list` | agent | `work:relay` | Membership (`project_membership`) |  |
| `workspace.shared_write.clear` | agent | `work:relay` | Identity rule `shared_write_agents_only` | An agent clears only its own entry. |
| `worker.publish` | agent | `work:relay` | An agent's channel; not a person's |  |
| `worker.heartbeat` | agent | `work:relay` | An agent's channel; not a person's |  |
| `control.pull` | agent | `work:relay` | An agent's channel; not a person's |  |
| `control.acknowledge` | agent | `work:relay` | An agent's channel; not a person's |  |
| `control.refuse` | agent | `work:relay` | An agent's channel; not a person's |  |
| `control.discard_complete` | agent | `work:relay` | An agent's channel; not a person's |  |
| `control.worker_settle` | agent | `work:relay` | An agent's channel; not a person's |  |
| `mail.route` | agent | `work:relay` | An agent's channel; not a person's | Worker-to-worker mail needs a project both workers attend. |
| `mail.reconciliation.list` | agent | `work:observe` | Membership (`project_membership`) |  |
| `mail.reconciliation.read` | agent | `work:observe` | Membership (`project_membership`) |  |
| `mail.reconciliation.publish` | agent | `work:relay` | An agent's channel; not a person's |  |
| `assignment.report` | work | `work:relay` | An agent's channel; not a person's | Closes only the ownership version it was issued for; old-period reports cannot restart closed execution, while recorded reports replay unchanged and an explicit authorized reopen permits its new ownership's reports. A completed report says what the reviewer can look at and what could not be verified. |
| `plan.nodes.publish` | plan | `work:relay` | An agent's channel; not a person's |  |
| `plan.index.embed` | plan | `work:relay` | Role AND current effective Card hierarchy | Spends on accounted embeddings. |
| `event.publish` | agent | `work:relay` | An agent's channel; not a person's | Applied publication requires the worker/coordinator role AND both live Cards. A currently registered limbo sender's bounded, linked-project attempt remains board-owned ignored evidence, never an applied domain effect; current work identity, privacy, size and immutable replay fences still apply. |
| `journal.view.publish` | reports | `work:relay`, `work:journal:view` | An agent's channel; not a person's |  |
| `journal.view.fail` | reports | `work:relay`, `work:journal:view` | An agent's channel; not a person's |  |
| `project.file.edit.result` | reports | `work:relay`, `work:journal:view` | An agent's channel; not a person's | Accepted once, from the coordinator the edit was addressed to. |
| `note.view.publish` | reports | `work:relay` | An agent's channel; not a person's |  |
| `note.view.fail` | reports | `work:relay` | An agent's channel; not a person's |  |
| `project.report.publish` | reports | `work:relay` | Identity rule `coordinator_publishes_reports` |  |
| `project.report.fail` | reports | `work:relay` | Identity rule `coordinator_publishes_reports` |  |
| `session.resume.publish` | agent | `work:relay` | An agent's channel; not a person's |  |
| `session.resume.fail` | agent | `work:relay` | An agent's channel; not a person's |  |
| `attachment.request_upload` | agent | `work:relay` | An agent's channel; not a person's | A person attaches through the board page, not this operation. |

## Operations of the board page

The board's own page calls these for a signed-in person. An alias uses its stated
canonical Card permission rather than a second permission named after the page call.

| Operation | For a person | Business rules |
| --- | --- | --- |
| `projects.list` | Membership (`project_membership`) | The caller's own projects. |
| `projects.resolve` | Membership (`project_membership`) | Resolves an exact project address without falling back to another project. |
| `project.connect_machine` | Membership (`project_membership`) | Read-only connection instructions; without a project, a signed-in person receives board-level instructions. |
| `telegram.bot_info` | Membership (`project_membership`) | Reads the configured bot's topic settings. |
| `project.people.list` | Membership (`project_membership`) | Project membership and Card reconciliation retain their own authority fences. |
| `project.people.card` | Membership (`project_membership`) | An admin reads another person's Card projection; other members read only their own. |
| `project.people.card.reset` | Membership (`project_membership`) | A person resets only their own My Card's Problem Board service to what their Control allows: preview shows the exact result, and confirm commits only that shown result in one PB/Hub transaction (work_card_transaction_card_reset_display_changed when it moved). |
| `project.people.control.backfill` | Membership (`project_membership`) | W665: a project admin adds to a person's Control Card the Problem Board operations of their committed role it is missing (add-only; managed, opt-in, explicit-admin and work:admin operations excluded): preview shows the exact addition, and confirm commits only that shown addition through the one W638 Card-edit transaction (work_card_transaction_role_backfill_display_changed when it moved). |
| `project.file.edit.request` | Role AND current effective Card hierarchy | A signed-in person needs project.files.edit and control.enqueue; edits only a file listed on this project. |
| `project.file.edit.get` | Membership (`project_membership`) | The requesting person's own edit result. |
| `work.attachment.add` | Role AND current effective Card hierarchy | plan.item.update through the current effective Card hierarchy, before promoting the staged upload; revision-fenced. |
| `work.notes.list` | Membership (`project_membership`) | An alias of work.notes.request with the same scope and view ownership. |
| `worker.share` | Identity rule `worker_pool_is_its_grantors` | The agent's owner shares its Card through Connection Hub at view or edit level; this is a global workpool boundary. |
| `worker.unshare` | Identity rule `worker_pool_is_its_grantors` | The agent's owner removes an exact share through Connection Hub; this is a global workpool boundary. |
| `worker.shares` | Identity rule `worker_pool_is_its_grantors` | The agent's owner reads its shares in Connection Hub. |
| `worker.share.candidates` | Identity rule `worker_pool_is_its_grantors` | Reads people on the caller's operating projects. |
| `board.get` | Membership (`project_membership`) |  |
| `timeline.list` | Membership (`project_membership`) | Inbox rows only for the people who read the inbox; a private thread only for its person. |
| `workers.list` | Membership (`project_membership`) |  |
| `workers.archive` | Membership (`project_membership`) | Your own retired agents, read-only; a platform admin may ask for everyone's. |
| `workers.card_reconciliation` | Identity rule `worker_pool_is_its_grantors` | Proposes retired agents whose Card is still live; revokes only the owner's exact agents whose confirmation token is sent back (a platform admin's everyone view is read-only). |
| `worker.delete` | Identity rule `worker_pool_is_its_grantors` | Permanent; only a retired agent whose Card is revoked, with its name and "delete permanently" typed back. Leaves an attributable tombstone. |
| `workers.search` | Identity rule `operator_inbox_people_only` |  |
| `inbox.list` | Identity rule `operator_inbox_people_only` |  |
| `inbox.summary` | Identity rule `operator_inbox_people_only` |  |
| `inbox.thread` | Identity rule `operator_inbox_people_only` |  |
| `inbox.thread.page` | Identity rule `operator_inbox_people_only` |  |
| `inbox.read` | Identity rule `operator_inbox_people_only` | Each person has their own read state. |
| `inbox.reply` | Identity rule `operator_inbox_people_only` | A reply to an agent is control.enqueue, decided on the person's Card. |
| `inbox.thread.privacy` | Identity rule `private_thread` | Audited; a private thread is visible only to its person. |
| `journal.view.get` | Membership (`project_membership`) |  |
| `journal.views.list` | Membership (`project_membership`) |  |
| `project.github_access` | Identity rule `person_views_people_only` | A person on the project reads their own GitHub key on the project, from Connection Hub under their session; it carries no token. |
| `review.history.list` | Membership (`project_membership`) | The signed-in person pages only review decisions they made. |
| `work.back_to_todo` | Identity rule `project_admin_by_role` | Only a Working item with no active assignment, with a reason kept on it; assigning and releasing still never change status, and a nonterminal status edit preserves execution state. An agent uses work.status.set with status todo. |
| `project.file.view.request` | Role AND current effective Card hierarchy | An operating person role (not viewer) AND control.enqueue through the current effective Card hierarchy, before creating the expiring view or queueing the coordinator. This is a queued request, not a pure read. Only a file on this project's list; the board keeps no durable content. |
| `project.people.history` | Membership (`project_membership`) | Thread privacy events are not shown. |
| `project.people.invitation.withdraw` | Identity rule `project_admin_by_role` |  |
| `project.people.remove` | Identity rule `project_admin_by_role` | The owner and the last admin are not removed; nobody removes themselves (last_admin). |
| `project.people.transfer_ownership` | Identity rule `last_admin` | Only the current owner, to another admin. |
| `project.report.request` | Identity rule `person_views_people_only` |  |
| `project.report.get` | Membership (`project_membership`) |  |
| `project.reports.list` | Membership (`project_membership`) |  |
| `project.report.archive` | Identity rule `person_views_people_only` |  |
| `project.report.restore` | Identity rule `person_views_people_only` |  |
| `project.report.delete` | Identity rule `person_views_people_only` |  |
| `session.resume.request` | Identity rule `worker_pool_is_its_grantors` | Only an agent the caller owns. |
| `session.resume.get` | Identity rule `person_views_people_only` |  |
| `session.resume.close` | Identity rule `person_views_people_only` |  |
| `work.cancel` | Role AND current effective Card hierarchy | A review decision: review.cancel on the person's Card, with its rules. |
| `work.command.get` | Identity rule `person_views_people_only` |  |
| `work.note.append` | Role AND current effective Card hierarchy | plan.note.append on the person's Card. |
| `work.notes.request` | Membership (`project_membership`) |  |
| `work.notes.get` | Identity rule `person_views_people_only` |  |
| `work.notes.close` | Identity rule `person_views_people_only` |  |
| `work.retag` | Role AND current effective Card hierarchy | plan.item.update on the person's Card, under the item's current revision. |
