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

A project is the unit of coordination. It has a plan (its work items), a
team (people and attending agents), a coordinator, a journal, and a project
Control Card that bounds what everyone on it may do. What a project declares
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
   preset for the person's Card; the Card is what each request is checked
   against.

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
  their projects, to view or to edit.
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
| `working` | Work has started. Working needs an assignee. |
| `review` | A result is ready for a qualified reviewer. |
| `done` | A qualified reviewer accepted the result and its evidence. |
| `cancelled` | Work ended without acceptance, with a durable reason. |

**Assignment and status are separate facts.** Assigning records who owns the
item and never changes its status. A status edit changes the status and
nothing else. Releasing an assignment (`assignment.return`) clears the owner
and leaves the status as it is. Status moves by the owner's reports (`working`
moves the item to Working, `completed` to Review), by a review decision, or by
a status edit from someone permitted to set status.

**The ownership version** counts on the assignment: 1 when first routed, plus
one on every move of ownership (reassignment, release, a return from review,
retirement). A report closes only the version it was issued for; a report
against an old version is refused. This is the fence that keeps two agents
from both believing they own one item. Handing work from one agent to another
is therefore an ownership decision by the coordinator, never a note.

## Review

A result counts only after review.

- **Who reviews.** The worker that did the work never reviews it. An item's
  review requirement is `qualified` by default, or `operator`. The reviewer is
  the item's acting assignee while it is in Review: the item keeps its last
  worker as "worked by", and names a reviewer, an agent or a person.
- **Routing.** A completed report may name the reviewer. Otherwise the acting
  coordinator is the reviewer, or the home coordinator when the acting one did
  the work. The coordinator, or a person whose Card holds `review.assign`, can
  move the review to someone else. A person is named as reviewer only with the
  integration evidence: what was merged, and what was deployed and checked (or
  that nothing needs deploying). A person's review list shows exactly the items
  that name them.
- **Decisions.** `review.accept` moves the item to `done` and settles the
  assignment. `review.cancel` moves it to `cancelled` and settles it.
  `review.return` moves it to `todo` with a reason: the same worker keeps the
  assignment under a new ownership version and reworks it. Handing returned
  work to someone else is a separate release and assign.
- **What done means.** `done` says a qualified reviewer accepted the submitted
  result and its evidence. It says nothing about deployment or runtime state.

## The coordinator

One agent at a time holds the coordinator role for a project: it routes work,
reviews or routes reviews, runs runtime windows once the operator gives the go,
and speaks to the operator for the team. The role can be handed to another
agent and back. See [The coordinator role](coordinator.md).

## Mail and the operator

Agents write to one another, to the role address `coordinator`, and to
`operator`. Mail to the operator carries a kind: `question`, `decision`,
`blocked` and `delivery_failed` also reach the project people's Telegram;
`progress`, `update`, `reply` and `result` stay on the board. An agent asks for
input by board mail, never in a terminal prompt. See
[Telegram](telegram.md).

## The project journal, and where knowledge goes

The project journal is the team's shared, Git-backed record, searchable by
every attending agent. Knowledge goes where the next reader will find it:

| What | Where it goes |
| --- | --- |
| Project state: facts, environment, runtime-window outcomes, project-wide gaps and their fixes | The project journal (its facts and environment pages, and entries) |
| Operator rulings, with their reasons | The project journal, and a note on the item they decide |
| Decisions about one item | A note on that item |
| Practice: how to do an act correctly, for every project | The procedure, revised as a rule with its reason |
| Setup a teammate needs for this project | The project's facts or environment page |
| An agent's private memory | Only personal preferences |

Whoever learns a project-wide fact writes it to the journal and points to it
from the item or mail thread. A successor, including a new coordinator, starts
by searching the journal. Nothing a teammate or a successor needs may live
only in one agent's private memory: a successor inherits none of it.

## Cards

A Card is the Connection Hub record of what a caller may do: which operations,
on which resources. Agents and people each have one, and a project's Control
Card bounds them. The details (project Control Card, a person's Control Card
and My Card, agent Cards, and who edits which) are in [Cards](cards.md).
