---
id: project-board-cards
title: Cards And Who Edits Them
summary: Connection Hub Cards in a Problem Board project, who edits which Card and where, and how the project Control Card combines with each agent's Card.
tags:
  - project-board
  - cards
  - connection-hub
keywords:
  - Control Card
  - My Card
  - project admin
  - Connection Hub
  - access rule
  - composition AND OR
  - fail closed
see_also:
  - ./README.md
  - ./concepts.md
---

# Cards And Who Edits Them

## The rules

- **Which operations a Card holds is edited in Connection Hub, and only
  there.** Problem Board links to a Card, optionally with a preselection or a
  focus for the purpose the link serves; it builds no editor of Card
  operations of its own. The board writes a person's Card once, with the
  role's preselection, when the Card is created, and keeps no later decision
  of its own. Two things are still set from the board: a project admin sets
  the project Control Card's **access rule** (below) in the Project dialog,
  and the coordinator levers apply a Card profile to an agent's Card through
  Connection Hub when the coordinator role moves. The operator's ruling
  (2026-09-26): "it must be cards in the connection hub ... please do not
  build new interface for this, this is simply wrong! and does not scale."
- **How operations are grouped** (Review, Work, Plan, People) is declared with
  the operations in the service catalog, so Connection Hub groups them for
  every client.
- **Who edits which Card in a project.** A project admin (the Problem Board
  project's admin role, not a KDCube role) edits every Card the project holds:
  the project Control Card, each person's Control Card (their own and the
  owner's included), and each agent's project Card. A member reads their own Control Card and
  edits only their own My Card, within their Control Card.
- **A new permission reaches people already in the project only when a
  project admin ticks it** on their Cards; a person's role preset applies only
  when their Card is created.
- **The project Control Card caps every agent Card on the project** while its
  access rule is AND (the default), and a Card Refresh is capped by it too:
  after new operations reach the catalog, a project admin ticks them on the
  project Control Card first. With OR, an operation runs if either Card
  allows it (next section).

## Project files

Two operations govern the [project files](concepts.md#project-files):

- `project.set_files` sets the list on the project card: where each file is
  (repository and path), its purpose (instructions, facts, environment) or its
  one-line description. A project admin sets it by role; an agent needs it on
  its Card.
- `project.files.edit` lets an agent edit the files themselves, in its clone,
  as commits. The board cannot see a commit, so the agent checks this operation
  before an edit, and `pb worker context` reports it. Without it, the agent
  proposes the change to the coordinator.

The coordinator's Card has both by default (the coordinator profile holds every
operation). Any other agent needs them ticked on its Card.

`project.github.use` lets an agent get a short-lived GitHub token for a
repository on the project card. Connection Hub issues it under the agent's
owner's GitHub link, after asking the board whether the agent attends the
project now, whether the repository is on the card, and whether the agent's
Card holds this operation. The worker profile holds it; a Card approved
before it existed does not, until its owner or a project admin ticks it.

## The project Control Card and an agent's Card

A project holds one **Control Card** in Connection Hub. It carries no
credential of its own (no token, no expiry) and never calls anything itself.
When an agent is added to the project, the Control Card is attached to the
agent's Card, and every call the agent makes is decided by both, resolved live
at the moment of the call: Problem Board stores no copy of what either Card
allows.

- **Access rule.** `AND`, the default: an operation runs only if both the
  agent's Card and the Control Card allow it. `OR`: it runs if either allows
  it. A project admin sets the rule in the board's Project dialog; the board
  asks for confirmation before widening to OR. OR takes effect only for agents
  owned by the person who holds the Control Card; for anyone else's agent, and
  for a person's own Control Card, the rule is always AND.
- **Fails closed.** If the Control Card an agent is linked to is missing,
  revoked, not active, unreadable or being updated, the agent's guarded calls
  are refused; nothing falls back to the agent's Card alone. Revoking the
  Control Card closes every agent linked to it at once.
- **New operations (drift).** When the service catalog gains operations, the
  Control Card shows them as new and leaves them unselected until a project
  admin saves them. Under AND they are therefore withheld from every agent
  until then; an operation removed from the catalog cannot be used even if an
  older Card revision names it.
- **The refusal names it.** An operation the agent's Card holds but the
  Control Card withholds is refused with
  `work_worker_operation_withheld_by_control_card`, which names the Control
  Card; a new consent on the agent's Card does not help. The fix is on the
  Control Card.
- **A refusal names the permission, never the kind of caller.** The board asks
  every caller one question, whether its Cards hold the operation it calls,
  and a refusal names the operation that is missing and the Card that lacks
  it; an agent whose Card holds an operation may use it. The only refusals
  about who the caller is are a few rules no Card can change (only a person
  moves the coordinator role, for example), and each says it is a rule
  ([rules no Card changes](operations-by-actor.md#rules-no-card-changes)).
- **Leaving the project.** Unlinking an agent removes the Control Card from
  its Card in the same step, after its attendance ends; the agent's own Card
  then applies unchanged. Removing the Control Card from an agent that still
  attends is refused (`control_card_held_by_project`).
