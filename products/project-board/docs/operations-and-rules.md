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

Every decision on the board is an operation on the caller's Card, with that operation's
business rules, or a stated identity rule (W260). An **agent** is always decided by the
operation on its own Card, checked by the catalog, with the same business rules. For a
**person**, the column says what decides it: their project Card (the owner may always,
`owner_exempt_from_card`), membership of the project for reads, or a named identity rule.
Which actor should hold each operation is in [Operations By Actor](operations-by-actor.md).

## Identity rules

| Rule id | Rule |
| --- | --- |
| `project_membership` | A person on the project reads it: the plan, items, notes, reports, people, workers, the board and the timeline. Membership scopes a person to a project; it is not an operation on a Card. |
| `operator_inbox_people_only` | The operator inbox is for people: its threads, replies, read state and worker directory are read and written by a signed-in person on the project (not an older read-only viewer), never by an agent. |
| `private_thread` | A thread its person made private is visible only to that person, the timeline included. |
| `person_views_people_only` | The views the board opens on a person's screen (a project report, a note view, a session-resume view, a command to the local plan host) are requested and managed by a signed-in person on the project; agents publish them. |
| `project_admin_by_role` | A project admin by role (owner or admin in this project) decides every Card on the project in Connection Hub, the project Control Card, the coordinator levers, the people on the project and its configuration (repositories, journal home, commit identity), however narrow their own Card. They stay admin until the admin role itself is removed. |
| `project_owner_is_a_person` | A project is registered by, and owned by, a signed-in person. |
| `owner_exempt_from_card` | The project's owner may do every Card operation on their project, whatever their Card holds, so a project always has someone who can act. |
| `last_admin` | The last admin of a project can be neither removed nor demoted, and the owner is not removed; ownership moves only by transfer to another admin. |
| `own_role` | A person does not change their own project role. |
| `worker_pool_is_its_grantors` | A worker belongs to the pool of the person who authorized it; another person reaches it only through a share or a project it is linked to. |
| `shared_write_agents_only` | Shared workspace writes are made by agents, each as itself. |
| `coordinator_publishes_reports` | A project report is published, or failed, by the project's coordinator. |
| `attachment_upload_people_only` | Attachments are staged by a signed-in person. |

## Operations a Card holds

| Operation | Group | Permission | For a person | Business rules |
| --- | --- | --- | --- | --- |
| `project.register` | project | `work:coordinate` | Identity rule `project_owner_is_a_person` | The signed-in person who registers a project is its owner and first admin. |
| `project.set_journal_home` | project | `work:coordinate` | Identity rule `project_admin_by_role` | Read only while the repository preset names no journal repository with a path. |
| `project.set_repositories` | project | `work:coordinate` | Identity rule `project_admin_by_role` | Written under the preset's current revision. Once the preset is the journal home, a repository keeps the journal role and a path. |
| `project.set_commit_identity` | project | `work:coordinate` | Identity rule `project_admin_by_role` | One email address, or empty to clear, under the preset's current revision, which it advances. |
| `project.set_files` | project | `work:coordinate` | Identity rule `project_admin_by_role` | An ordered list of repository alias and path, each alias one of the project's repositories; the instructions, facts and environment purposes at most once each; written under the list's current revision. |
| `project.files.edit` | project | `work:coordinate` | Membership (`project_membership`) | A read-only check an agent makes before editing the project's files in its repositories; the coordinator profile holds it. |
| `project.plan.index` | plan | `work:observe` | Membership (`project_membership`) |  |
| `project.plan.item` | plan | `work:observe` | Membership (`project_membership`) |  |
| `project.plan.resolve` | plan | `work:observe` | Membership (`project_membership`) |  |
| `project.plan.search` | plan | `work:observe` | Membership (`project_membership`) |  |
| `project.plan.import` | plan | `work:coordinate` | Their project Card | Replaces the whole plan from a complete package. The owner may always (owner_exempt_from_card). |
| `project.references.preview` | plan | `work:coordinate` | Their project Card | Changes nothing. The owner may always (owner_exempt_from_card). |
| `project.references.migrate` | plan | `work:coordinate` | Their project Card | Applies exactly the rewrite a preview returned. The owner may always (owner_exempt_from_card). |
| `project.plan.embedding_status` | plan | `work:observe` | Membership (`project_membership`) |  |
| `plan.item.create` | plan | `work:coordinate` | Their project Card | The owner may always (owner_exempt_from_card). |
| `work.status.set` | work | `work:coordinate` | Their project Card | Working needs an assignee; entering review needs review.look_at and review.could_not_verify. The assignee and the assignment stay as they are. The owner may always (owner_exempt_from_card). |
| `work.accept` | work | `work:review` | Their project Card | An alias of review.accept, with its rules. |
| `review.accept` | review | `work:review` | Their project Card | No one accepts their own work (work_review_self_forbidden). An agent decides a review only as the item's named reviewer or the acting coordinator (work_review_not_reviewer). |
| `review.return` | review | `work:review` | Their project Card | A reason is required. An agent decides a review only as the item's named reviewer or the acting coordinator (work_review_not_reviewer). |
| `review.cancel` | review | `work:review` | Their project Card | A reason is required. An agent decides a review only as the item's named reviewer or the acting coordinator (work_review_not_reviewer). |
| `review.assign` | review | `work:coordinate` | Their project Card | Routes only an item in review. The owner may always (owner_exempt_from_card). |
| `project.people.invite` | people | `work:admin` | Identity rule `project_admin_by_role` | An existing KDCube user, by email, with a role; their Control Card starts with that role's preselection. |
| `project.people.set_role` | people | `work:admin` | Identity rule `project_admin_by_role` | A role writes no Card. A person does not change their own role while no other admin remains (last_admin, own_role). |
| `project.people.card.update` | people | `work:admin` | Retired | Answers work_control_card_edit_in_connection_hub: Cards are edited in Connection Hub. |
| `project.coordinator.get` | coordinator | `work:observe` | Membership (`project_membership`) | Agents also receive it on every heartbeat. |
| `project.coordinator.hand_over` | coordinator | `work:admin` | Identity rule `project_admin_by_role` | A signed-in person only; revision-fenced. |
| `project.coordinator.return` | coordinator | `work:admin` | Identity rule `project_admin_by_role` | A signed-in person only; revision-fenced. |
| `project.coordinator.set_away` | coordinator | `work:admin` | Identity rule `project_admin_by_role` | Away reads as unavailable whatever the session reports. |
| `project.coordinator.note.write` | coordinator | `work:coordinate` | An agent's channel; not a person's | The coordinator holder only (work_coordinator_note_not_holder); every section is required. |
| `project.coordinator.make` | coordinator | `work:admin` | Identity rule `project_admin_by_role` | A signed-in person only. |
| `project.coordinator.make_worker` | coordinator | `work:admin` | Identity rule `project_admin_by_role` | Refused for the home coordinator's own Card. |
| `plan.item.update` | plan | `work:coordinate` | Their project Card | Under the item's current revision; new attachment refs must be staged uploads. The owner may always (owner_exempt_from_card). |
| `plan.item.delete` | plan | `work:coordinate` | Their project Card | Only an unassigned leaf item. The owner may always (owner_exempt_from_card). |
| `plan.note.append` | plan | `work:coordinate` | Their project Card | The owner may always (owner_exempt_from_card). |
| `plan.notes.list` | plan | `work:observe` | Membership (`project_membership`) |  |
| `project.control.get` | control | `work:observe` | Membership (`project_membership`) |  |
| `project.control.initialize` | control | `work:coordinate` | Identity rule `project_admin_by_role` | Once per project. |
| `project.control.update` | control | `work:coordinate` | Identity rule `project_admin_by_role` | Each edit is in People History with who made it. |
| `journal.view.request` | reports | `work:observe` | Membership (`project_membership`) | The project must have a journal home. |
| `journal.view.close` | reports | `work:observe` | Membership (`project_membership`) | Erases the requesting person's own snapshot. |
| `project.link_worker` | project | `work:coordinate` | Their project Card | Whose agent it is: its owner's, or shared with the caller (worker_pool_is_its_grantors). Linking the project's first coordinator creates its Control Card, which only a project admin does. The owner may always (owner_exempt_from_card). |
| `project.unlink_worker` | project | `work:coordinate` | Their project Card | The agent's owner unlinks it, or a project admin unlinks any agent. The project's Control Card comes off after the attendance stops. The owner may always (owner_exempt_from_card). |
| `worker.rename` | agent | `work:coordinate` | Identity rule `worker_pool_is_its_grantors` | An agent renames only itself; a person only an agent it owns. |
| `worker.estimate` | agent | `work:relay` | Identity rule `worker_pool_is_its_grantors` | An agent states its own; its owner may state it. |
| `worker.evict` | agent | `work:coordinate` | Their project Card | Only an agent the caller owns (worker_pool_is_its_grantors), linked to the project. The owner may always (owner_exempt_from_card). |
| `worker.restore` | agent | `work:coordinate` | Their project Card | Only an agent the caller owns (worker_pool_is_its_grantors), linked to the project. The owner may always (owner_exempt_from_card). |
| `worker.retire` | agent | `work:coordinate` | Identity rule `worker_pool_is_its_grantors` | Permanent; the retirement names the agent to confirm it. |
| `control.enqueue` | agent | `work:coordinate` | Their project Card | Only to an agent linked to the project, the caller's own or shared with them. Attachments are staged by a person (attachment_upload_people_only). The owner may always (owner_exempt_from_card). |
| `control.discard` | agent | `work:coordinate` | Its business rule | Discards only messages the caller sent (work_control_discard_target_denied). |
| `assignment.assign` | work | `work:coordinate` | Their project Card | Also reopens review, done or cancelled work; assignment never changes status. The owner may always (owner_exempt_from_card). |
| `assignment.return` | work | `work:coordinate` | Their project Card | The owner's reason is required; the item keeps its status. The owner may always (owner_exempt_from_card). |
| `assignment.list` | work | `work:relay` | Membership (`project_membership`) | An agent pages its own assignments. |
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
| `assignment.report` | work | `work:relay` | An agent's channel; not a person's | Closes only the ownership version it was issued for. A completed report says what the reviewer can look at and what could not be verified. |
| `plan.nodes.publish` | plan | `work:relay` | An agent's channel; not a person's |  |
| `plan.index.embed` | plan | `work:relay` | Membership (`project_membership`) | Spends on accounted embeddings. |
| `event.publish` | agent | `work:relay` | An agent's channel; not a person's |  |
| `journal.view.publish` | reports | `work:relay`, `work:journal:view` | An agent's channel; not a person's |  |
| `journal.view.fail` | reports | `work:relay`, `work:journal:view` | An agent's channel; not a person's |  |
| `note.view.publish` | reports | `work:relay` | An agent's channel; not a person's |  |
| `note.view.fail` | reports | `work:relay` | An agent's channel; not a person's |  |
| `project.report.publish` | reports | `work:relay` | Identity rule `coordinator_publishes_reports` |  |
| `project.report.fail` | reports | `work:relay` | Identity rule `coordinator_publishes_reports` |  |
| `session.resume.publish` | agent | `work:relay` | An agent's channel; not a person's |  |
| `session.resume.fail` | agent | `work:relay` | An agent's channel; not a person's |  |
| `attachment.request_upload` | agent | `work:relay` | An agent's channel; not a person's | A person attaches through the board page, not this operation. |

## Operations of the board page

The board's own page calls these for a signed-in person; no Card holds them.

| Operation | For a person | Business rules |
| --- | --- | --- |
| `projects.list` | Membership (`project_membership`) | The caller's own projects. |
| `board.get` | Membership (`project_membership`) |  |
| `timeline.list` | Membership (`project_membership`) | Inbox rows only for the people who read the inbox; a private thread only for its person. |
| `workers.list` | Membership (`project_membership`) |  |
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
| `project.file.view.request` | Membership (`project_membership`) | Only a file on the project's list, served by an agent that attends the project; the board keeps no content, only an expiring view stamped with its commit. |
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
| `work.cancel` | Their project Card | A review decision: review.cancel on the person's Card, with its rules. |
| `work.command.get` | Identity rule `person_views_people_only` |  |
| `work.item.save` | Their project Card | Each step is checked for its own operation: the fields, the assignment and the status. |
| `work.note.append` | Their project Card | plan.note.append on the person's Card. |
| `work.notes.request` | Membership (`project_membership`) |  |
| `work.notes.get` | Identity rule `person_views_people_only` |  |
| `work.notes.close` | Identity rule `person_views_people_only` |  |
| `work.retag` | Their project Card | plan.item.update on the person's Card, under the item's current revision. |
