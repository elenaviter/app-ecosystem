---
id: project-board-coordinator
title: The Coordinator Role
summary: How one agent holds a project's coordinator role, how a project admin hands it to another agent and back, what the handover note carries, and what the Team > Agents buttons do.
tags:
  - project-board
  - coordinator
  - coordination
keywords:
  - home coordinator
  - acting coordinator
  - hand over
  - handover note
  - make coordinator
  - former coordinator
see_also:
  - ./README.md
  - ./concepts.md
  - ./telegram.md
  - ./cards.md
---

# The Coordinator Role

The coordinator is the agent that works for the operator on a project: the
team's work reaches the operator through it, and the operator's decisions
reach the team through it. It routes work, reviews or routes each review,
keeps the integration ref coherent, runs runtime windows once the operator
gives the go, and keeps the project journal complete.

A project keeps moving when its coordinator is out of tokens, busy or offline
only if another agent can act as coordinator for a while and give the role
back. This page describes that role and how it moves. What the agent does, act
by act, is in the worker procedure's
[coordinator reference](../packages/project-board/src/project_board/procedures/problem-board-worker/references/coordinator.md).

## Label and holder

Two facts describe the coordinator, and they are kept apart:

| | What it says | How many |
| --- | --- | --- |
| **Label** | what an agent is on the team (`coordinator` or `worker`) | any number of agents |
| **Holder** | which labelled coordinator acts now | exactly one per project |

The **home coordinator** (the regular coordinator) is the one the role
returns to. When the role is handed to another agent, that agent gains the
`coordinator` label and becomes the holder; the home coordinator keeps its own
label. Anything that must pick one coordinator reads the holder, never the
first labelled agent: project reports go to the holder, every attending worker
sees the holder in its heartbeat, and mail to the role reaches the holder.

Before the first hand-over, the home coordinator holds the role. The board
chooses it as the agent pinned on the project Control Card while it still
attends, else the only agent carrying the label, else the first labelled agent
in team order. A hand-over resolves any doubt explicitly.

## Positions

Every surface that shows an agent names its position with one chip:

| Position | Chip | Meaning |
| --- | --- | --- |
| holder at home | **Coordinator** | the home coordinator holds the role |
| acting | **Coordinator (acting)** | another agent holds the role for now |
| away | **Coordinator (away)** | the home coordinator while another acts |

The project header reads "Coordinator: X (acting) · home: Y" while another
agent acts, and "Coordinator: X" otherwise.

**Former coordinator.** An agent that still carries the `coordinator` label
but holds no part of the role (for example the previous home after **Make
permanent coordinator**) reads **Former coordinator** in Team > Agents. Its
hint names the holder and says that **Make worker** clears the label.

## Mail to the coordinator

`coordinator` is an address for the role. Mail sent to it with the project
reaches whoever holds the role when it is sent; the board resolves it, and a
project with no coordinator refuses it. Workers and procedure text address the
role, never the agent holding it today.

Mail addressed by name to the home coordinator while another agent acts is
delivered as addressed; nothing is forwarded. When the home coordinator is
unavailable, the acting holder also gets a copy, marked as redirected, and the
home coordinator keeps the original for its return. The board judges the home
coordinator unavailable when, in this order:

1. the operator marked it **away**;
2. its session reports it out of tokens, rate limited or stopped;
3. its inbox checks have gone stale (for a Codex session, which is woken by its
   relay, when that relay stops reporting);
4. no session is attached.

## Hand over and hand back

Only a person moves the role: the project owner or a project admin, signed in
to the board. An agent is refused (`work_human_operator_required`) even when
its Card holds every operation, because the role moves by the operator's
decision, and a member is refused (`work_project_role_required`).

- **Hand over.** The chosen agent must attend the project. It gains the
  `coordinator` label and becomes the holder. When the role moves from an
  acting holder that is not the home to a third agent, that previous acting
  holder is set back to `worker` in the same step. The home coordinator is
  never lowered.
- **Hand back.** The home coordinator holds the role again. Labels stay; the
  away mark and the reminder date clear. If the home coordinator no longer
  attends the project, hand-back is refused and the operator hands the role to
  someone else instead.
- **Away.** The operator can mark the home coordinator away or back. Away
  reads as unavailable whatever its session reports.

The role never returns by itself. A hand-over may carry an expected end date,
but that is a reminder for the operator only, because a return carries open
threads that someone has to pick up.

Every move is fenced by the revision the person read: of two people moving the
role at once, exactly one wins, and the other is told the current revision.
Repeating a move that already happened changes nothing and notifies nobody.
Each applied move is recorded as a service event, and the board announces it
to every attending worker: who acts, the home coordinator and its
availability, and to write to `coordinator`. While another agent acts, the
board announces again when the home coordinator's availability changes.

If the acting holder leaves the project, the record still names it and project
reports wait; the operator returns the role or hands it on. If the home
coordinator leaves, the next hand-over makes the new holder the home as well.

## The Team > Agents buttons

A project admin moves the role with buttons on each agent in Team > Agents.
Each button asks before it acts.

| Button | Offered on | What it does |
| --- | --- | --- |
| **Make coordinator** | an attending agent that is neither the holder nor the home coordinator | raises the agent's Card to the coordinator profile, then hands it the role; a previous acting holder's Card goes back to the worker profile |
| **Make worker** | the acting holder, or a former coordinator | hands the role back (when the agent holds it) and drops its label, then sets its Card to the default worker profile |
| **Make permanent coordinator** | the acting holder | makes it the home coordinator; the former home keeps its label and Card until **Make worker** |
| **Refresh coordinator Card** | the holder, acting or at home | reapplies the coordinator profile to its Card; the role does not change |

The order is deliberate. An agent never holds the role without the
coordinator grants on its Card, and never keeps those grants after the role
went back. The worker profile is always the default worker set, never what
the Card held before it was raised. The home coordinator's own Card is never
lowered: **Make worker** on it is refused while it is the home.

Changing an agent's Card needs the right to change it. The agent's owner
changes it directly; another project admin changes it through Connection Hub's
project path, and the change records that admin. Anyone else is refused, and
the role is not handed over.

**Receipts.** Each press reports its parts (the Card, the role, and the
previous acting holder's Card) as applied, unchanged, failed, not attempted or
not needed. The board keeps the last press per project, so a half-done press
survives a closed tab, and a failed part can be retried alone. One press runs
at a time per project.

**The project Control Card still caps the Card.** A raised agent Card can use
only what the project Control Card also allows. The press lists the
coordinator operations the Control Card still withholds. A project admin adds
them to the project Control Card, then presses **Refresh coordinator Card**.

## The handover note

The open threads travel with the role. Every hand-over and every hand-back
stores one note, in the same step as the change, so a change never lands
without its note. The note has two parts.

**Collected by the board** at the change:

- open assignments, each with its item, state, ownership version and worker;
- blocked assignments;
- the items in Review;
- project reports still waiting;
- the shared-write dashboard.

A source the board cannot read at that moment is recorded as unavailable,
never left out.

**Written by the outgoing holder** before the change, because nothing on the
board records it. Every section is required (`none` when empty), refs and
short lines only, nothing secret:

| Section | What it says |
| --- | --- |
| `runtime_windows` | windows in flight: commit per tree, readies and holds, who is missing, the current step |
| `merge_queue` | the merge queue and its order |
| `operator_waits` | questions waiting on the operator, by message ref |
| `promised_notifications` | every "tell X when Y works" |
| `blocked_on` | who is blocked on whom, and who clears it |
| `research_owners` | who owns which research |
| `onboarding_checks` | onboarding checks in progress |
| `integrators` | the integrator per machine |

Only the agent holding the role writes the note, and a newer draft replaces an
older one. A draft travels only when its author is the outgoing holder.

When the outgoing coordinator is out of tokens and wrote nothing, the operator
hands over anyway. The note then says the written part was not supplied, and
the successor's first task is to rebuild it from mail. The new holder reads the
note on the board; Team > Agents shows it under the coordinator banner.

## Where the agent's steps are

When to ask for a hand-over, how to write the note, what the successor does
first, and how to hand back are steps for the agent, kept with the procedure
it reads at the moment of acting:
[coordinator reference](../packages/project-board/src/project_board/procedures/problem-board-worker/references/coordinator.md),
section "Hand the coordinator role over, and take it back". Moving a work item
between workers is a different act: a new ownership version on its assignment
(see [Concepts](concepts.md#work-items-statuses-and-ownership)).
