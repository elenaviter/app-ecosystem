---
id: project-board-concepts
title: Problem Board Concepts
summary: What Problem Board is for, and the concepts it is built from, from projects, people and agents to work items, review, the coordinator and the project journal.
tags:
  - project-board
  - concepts
  - coordination
keywords:
  - project admin
  - attendance
  - assignment
  - ownership version
  - review
  - project journal
see_also:
  - ./README.md
  - ./coordinator.md
  - ./telegram.md
  - ./cards.md
  - ./topology-and-flows.md
  - ./projects-runtimes-and-refs.md
---

# Problem Board Concepts

## What Problem Board is, and why

Problem Board coordinates coding-agent sessions (Claude Code, Codex and
compatible agents) that people start themselves, across projects and
machines. A KDCube deployment serves the board: the plan, the mail between
agents and people, assignments, reviews and the authority checks. One relay
per machine connects the agent sessions on that machine to the board.

It exists so that several agents, on several machines, can work on one
project the way a team does: each knows what it owns, work is reviewed
before it counts as done, one coordinator routes and decides, and the people
who run the project are asked on the board rather than in a terminal they may
not be watching. The board never starts a model. It connects sessions that
people started to addressed work.

## Project

A project is the unit of coordination. It has a goal, a plan (its work
items), a team (people and attending agents), a coordinator, its
[project files](#project-files), an optional journal, and a project Control
Card that bounds what everyone on it may do. Creating one needs only a title;
the rest is added on the project card whenever the person wants. What a project declares
about its repositories and runtimes is in
[Projects, runtimes and refs](projects-runtimes-and-refs.md).

A project has its own address on the board site, by its readable name. The
address selects the project and grants nothing.

## People: members and project admins

Every person on a project has a role, `admin` or `member`, and the person who
registered the project is its **owner**.

- **A project admin** is an admin of that Problem Board project. It is a role
  inside the project, not a KDCube platform role and not a sign-in provider
  role: the board reads only the person's role in that project, so one account
  can be an admin on one project and a member on another. A project admin
  changes any project-held Card (their own and the owner's included), invites
  people, applies roles, and removes people.
- **A member** works in the project within what their Card allows, and edits
  only their own My Card, within their Control Card.

How a person becomes a project admin:

1. The creator of a project is its owner and a project admin automatically.
2. A project admin invites an existing KDCube user by the email of their
   account, choosing the role in the same step. The invitation is redeemed
   when that person opens the board signed in with that email and the sign-in
   provider has verified it.
3. A project admin can make any person on the project an admin. A role is a
   preset applied when the person's Card is created; after that the Card in
   Connection Hub is what each request is checked against, and only its
   editors change it.

A project admin stays admin until the admin role itself is removed: taking
operations off their Card does not make them a member. An admin can make
themselves a member only while another project admin remains, so a project
always has someone who can edit Cards. The owner's role is fixed; ownership
moves only when the owner transfers it to another admin.

In these pages, **the operator** means the people who own or administer a
project, as the agents see them: agents write to `operator`. Every person on
the project reads the project's agent conversations and can answer them,
unless the person in a thread made it private.

## Agents, sessions and the relay

An **agent** (a worker) is one coding-agent session that a person explicitly
joined to the board through the installed `problem-board-worker` skill. Other
sessions on the same machine stay ordinary repository agents.

- **Session.** The board binds a worker to one exact native session of its
  provider. The person who authorized it is its owner, and the agent joins that
  person's pool and nobody else's. The owner may share it with a person from
  their projects, to view or to edit. Another person sees the owner's display
  name only when they share a project with the owner, and otherwise "another
  person"; never the raw account id or an email.
- **Names.** Each worker has a stable worker name, used to address mail and
  assignments, and an alias, which is display text only.
- **Card.** The agent acts through its own Connection Hub Card, granted by its
  owner in the browser. Every operation it calls is checked against that
  Card, and against the project Control Card while it attends a project.
- **Relay.** One relay per machine carries mail and heartbeats between the
  board and the sessions on that machine. A Claude Code session keeps one
  background watch that tells it when mail is waiting; a Codex session is woken
  by its relay between turns. Notification, receiving and settling a message
  are separate steps. The flows are drawn in
  [Topology and flows](topology-and-flows.md).

A worker can be **detached** (the session stops participating), **suspended**
(reversible), or **retired** (its board identity is closed for good, its
history kept). Its Card stays a separate credential its owner reviews or
revokes.

## Attendance, assignment and direct conversation

These are three independent states. None implies another.

| State | What it means | Who changes it |
| --- | --- | --- |
| Direct conversation | The owner (or a person the agent is shared with) messages the agent, attending or not. | Anyone who may message it |
| Attendance | The agent is on a project's team: it gets the project's context, and teammates can address it. | Linking and unlinking the agent |
| Assignment | The agent owns one work item under an ownership version. | The coordinator, or a person whose Card allows it |

An idle agent that attends no project can still be messaged. The board offers
Link only for an idle agent; an agent attending another project is unlinked
there first. Unlinking ends only the attendance: the agent's conversation,
history, and attribution stay.

## Work items, statuses and ownership

A work item is one entry of the project's plan. The item is authoritative:
when a message and the item disagree, the item wins, and whoever sent the
message fixes the item.

| Status | Meaning |
| --- | --- |
| `todo` | Work has not started. The item may already have an assignee. |
| `working` | Work has started. The assignee may be empty; status does not decide it. |
| `review` | A result is ready for a qualified reviewer. |
| `done` | A qualified reviewer accepted the result and its evidence. |
| `cancelled` | Work ended without acceptance, with a durable reason. |

**Assignment and status are separate facts.** Assigning records who owns the
item and never changes its status. A status edit preserves the displayed
assignee and ownership version. Done and Cancelled close execution without a
review verdict; nonterminal status alone does not reopen closed execution.
An explicit authorized `assignment.assign(reopen=true)` is a separate act:
it creates a new ownership period without changing status or `started_at`.
The new owner's first working report then follows the normal report contract;
the former period remains fenced, including when the selected worker is unchanged.
The Board records this explicit act as ownership-bound `reopen_evidence`.
The client validates it against the current active assignment and selected
owner before emitting `expected_reaction=begin_work`; mail text alone is not
authority. Ordinary Review notices still await review, and Done/Cancelled
notices remain informational. A later closure or ownership change wins.
Releasing an assignment (`assignment.return`) clears the owner
and leaves the status as it is. Status moves by the owner's reports (`working`
moves the item to Working, `completed` to Review), by a review decision, or by
a status edit: by an agent whose Card holds `work.status.set`, or by any
person on the project, admin or member.

`item.assignee` is that owner in every status, including Review and Done.
Assignment history and reviewer routing never replace its displayed value.
The single current list on an agent or person Card contains the item exactly
when this field names them, including Done items. Former contributions are not
current membership and Cards offer no participation-history list.
Explicit assignee edits are valid at any status. A combined edit applies both
selected fields atomically. [Work Item Review](review.md#assignment-and-status-are-separate-facts)
owns the save and historical-assignment semantics.

**The ownership version** counts on the assignment: 1 when first routed, plus
one on every changed assignee (including a closed-owner clear), reassignment,
release, return from review or retirement. Terminal status alone does not
advance it. An unowned fence increment grants no reporting authority.
A report closes only the version it was issued for; a report
against an old version or closed execution is refused. This is the fence that keeps two agents
from both believing they own one item. Handing work from one agent to another
is therefore an ownership decision by the coordinator, never a note.

### Reading and editing across refresh

Opening an item from the plan, search results, an assignment list or a link
opens its complete detail, even outside the loaded plan page. Background board
polling and automatic search re-ranking keep that item's reading position and
staged edits mounted. Leaving the refreshed results is not deletion. Explicit
navigation or a confirmed deletion or access loss can close the detail; a
transient read failure cannot. Save remains revision-fenced: other plan writes
do not discard the draft, and a stale save cannot overwrite another writer's
item changes and retains the draft with an error.

### Item keys and files on an item

Every item has a short **key** such as `W343`, unique inside its project. The
key is how people and agents name an item in conversation, in branch names
(`work/w343-...`) and in commands: `project.plan.item` and
`plan.notes.list` accept `{"item_key": "W343"}`. It is matched without regard
to case and only inside the project named with it; the same key in another
project is another item. When an exact item state matters (a report, a
dependency, an audit), use the item's reference instead; see
[refs and identifiers](refs-and-identifiers.md).

**Files belong to the item, not to one of its states.** A file attached to an
item stays on it across edits until an edit removes it, and the item's
version changes when its files do. People attach files on the board; an agent
attaches one with `pb worker item-attach --project-ref <project> --item-key
<key> --file <path>` and reads one with `pb worker item-attachment-read`. An
agent can attach only a file it uploaded for that edit, and read only a file
the item lists. An item read names its files without links; each download asks
for one short-lived link (`work.attachment.link`), and the file reference is
what lasts ([delivery](delivery.md)).

## Review

A result counts only after review.

- **Who reviews.** The worker that did the work never reviews it. An item's
  review requirement is `qualified` by default, or `operator`. Reviewer
  qualification and routing evidence are recorded separately from the current
  fields. Review is a status, not a second current-owner control. The item
  editor always shows the same Status and Assignee fields.
- **Routing.** A completed report may name the reviewer. Otherwise the acting
  coordinator is the reviewer, or the home coordinator when the acting one did
  the work. The coordinator, or a person whose Card holds `review.assign`, can
  move the review to someone else. A person is named as reviewer only with the
  integration evidence: what was merged, and what was deployed and checked (or
  that nothing needs deploying). A person's Card uses the current-assignee
  list described above, not a second list selected by historical review roles.
- **Decisions.** `review.accept` moves the item to `done` and settles the
  assignment. `review.cancel` moves it to `cancelled` and settles it.
  `review.return` moves it to `working` with a reason: the same worker keeps the
  assignment under a new ownership version and reworks it. Handing returned
  work to someone else is a separate release and assign.
- **What done means.** `done` says a qualified reviewer accepted the submitted
  result and its evidence. It says nothing about deployment or runtime state.

## The coordinator

One agent at a time holds the coordinator role for a project: it routes work,
reviews or routes reviews, runs runtime windows once the operator gives the go,
and speaks to the operator for the team. The role can be handed to another
agent and back. See [The coordinator role](coordinator.md).

## Optional project roles

A project may also declare an optional role the board carries beside the
coordinator. The one role today is the knowledge keeper, which receives
hand-overs about finished work for the project's knowledge. Its states are
none, declared but unassigned, held by one agent, and held but unavailable;
`pb worker context` and Team show them next to the coordinator. Only a
person who is an owner or admin, with `project.role.manage` on their Card,
declares the role or names its holder, under the role's revision. No preset
ticks that operation. The role grants no permission.

A mail to `knowledge-keeper` about an item is a hand-over. The board keeps it
pending until the agent holding the role decides it with
`project.role.handover.decide`: incorporated, with the published result;
declined; or needing evidence. Settling the mail does not decide it. The
role's view shows how many hand-overs are pending, the oldest, and whether it
is overdue.

## Mail and the operator

Agents write to one another, to the role addresses `coordinator` and
`knowledge-keeper`, and to `operator`. Mail to the operator carries a kind: `question`, `decision`,
`blocked` and `delivery_failed` also reach the project people's Telegram;
`progress`, `update`, `reply` and `result` stay on the board. An agent asks for
input by board mail, never in a terminal prompt. See
[Telegram](telegram.md). A role address is resolved by the board when the mail
is sent, to the agent holding the role then; a role with no holder, or a holder
that left the project, is refused by name, never redirected.

## Project files

Project files are the project's shared, current knowledge. Every agent of the
project is told where they are, reads them in its own clone, and follows them.

They live in the project's repositories, never in the board. The project card
lists where each one is: a repository from the card and a path, with an
optional one-line description. As many as the person provides.

When an agent starts or joins the project, `pb worker context` gives it the
three purpose files (each with its path in this agent's clone and whether it is
there) and the list of further files with their one-line descriptions. The
agent reads them first, before any work:

- **Instructions:** what the project is, its rules and conventions, how work is
  done there.
- **Facts:** the decisions and rulings in force now.
- **Environment:** machines, runtimes, how to test and deploy.

It reads a further file when that file's description fits the task at hand.
The descriptions are a table of contents, not a reading list.

While it works: when a file changes, or one is added to the list, agents are
told on their next check and reread it. One edit changes what every agent on
every machine does. When the person makes a ruling, the coordinator writes it
into Facts: rulings live in project files, not in any agent's private memory.
Editing project files is a permission on the Card
([Cards](cards.md#project-files)); the coordinator has it by default. An agent
without it proposes the change to the coordinator.

The card shows a file's content through an agent of the project that is online:
its relay reads the file from its clone and the board shows it with the commit
it was read at. The board keeps no copy.

How this differs from a journal: project files are the current truth (what
applies now), and every project has them. A journal is history (what happened
and why), and it stays optional.

Example: a private monthly project.

- Instructions: `AGENTS.md` (drafts only, the originals untouched, nothing
  leaves the project).
- Facts: `facts.md` (the monthly deadline; the board holds statuses only).
- Environment: the tools the routine needs.
- A further file: `monthly-routine.md`, "The steps of one monthly pass".

The agent reads `AGENTS.md` before anything else and opens `monthly-routine.md`
when it starts a month's pass. A second agent added later knows all of this
from its first minute.

## The project journal, and where knowledge goes

The project journal is the team's shared, Git-backed record of what it
learned while working (why it chose this and not that, what failed and why,
what is still open), searchable by every attending agent. Knowledge goes where the next reader will find it:

| What | Where it goes |
| --- | --- |
| Facts and environment in force | The project's facts and environment files |
| Why the project chose this and not that, failures and their mechanisms, wrong assumptions, limits and open gaps | The project journal |
| Where the work stands: progress, heads, approvals, merges, runtime-window receipts | The work item's notes and reports |
| Operator rulings, with their reasons | The facts file while in force, a note on the item they decide, and the journal entry that explains what they changed |
| Decisions about one item | A note on that item |
| Practice: how to do an act correctly, for every project | The procedure, revised as a rule with its reason |
| Setup a teammate needs for this project | The project's facts or environment page |
| An agent's private memory | Only personal preferences |

Whoever learns a project-wide lesson writes it to the journal and points to it
from the item or mail thread. A successor, including a new coordinator, starts
by searching the journal. Nothing a teammate or a successor needs may live
only in one agent's private memory: a successor inherits none of it.

A project may also define an optional **knowledge keeper**: a role like the
coordinator's, held by an agent that keeps the project's knowledge base current
from finished work, fed by a hand-over after each item that changes what the
knowledge covers. The knowledge base says how things work now; the journal
stays the history of the work. What the role does, when a hand-over is owed and
what stays in the project's own files is in the worker procedure's
[knowledge keeper reference](../packages/project-board/src/project_board/procedures/problem-board-worker/references/knowledge-keeper.md).

## Cards

A Card is the Connection Hub record of what a caller may do: which operations,
on which resources. Agents and people each have one, and a project's Control
Card bounds them. The details (project Control Card, a person's Control Card
and My Card, agent Cards, and who edits which) are in [Cards](cards.md).
