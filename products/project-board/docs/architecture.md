---
id: project-board-architecture
title: Problem Board Architecture
summary: The pieces Problem Board is made of (the board served by a KDCube deployment, the pb client and the host relay, Connection Hub Cards, Telegram) and how every worker operation reaches the board through the relay under the worker's own Card, without the agent ever handling a credential.
tags:
  - project-board
  - architecture
  - authorization
  - relay
keywords:
  - control service
  - operation catalog
  - live-Card guard
  - pb coordinate
  - governed operation
  - host relay
  - Connection Hub
  - Control Card
  - shared-write dashboard
see_also:
  - ./README.md
  - ./topology-and-flows.md
  - ./operations-by-actor.md
  - ./cards.md
  - ./telegram.md
  - ./storage-and-retention.md
  - ./delivery.md
---

# Problem Board Architecture

Problem Board has four pieces: the board, served by a KDCube deployment; the
`pb` client and one relay per machine; Connection Hub, which holds every
Card; and Telegram, the urgent path to the people on a project. This page
says what each piece owns and how an operation travels between them. The
flows are drawn step by step in [Topology and flows](topology-and-flows.md).

```text
person in the browser ----> board site ------+
supervisor agent --------> Named Services ---+
                                             v
                            +--------------------------------+
                            | Problem Board control service  |
                            | one operation catalog          |
                            | live-Card guard on every call  |
                            +--------------------------------+
                                  ^                    |
             Card partition on    |                    | urgent mail
             the Data Bus         |                    v
                                  |              Telegram (private chat
+---------------------------------+--+           per person, topic per
| one relay per machine              |           project)
|  one channel per enrolled session  |
|  holds each session's credential   |
+------------------------------------+
        ^  local request / response queue
        |
coding-agent session: pb coordinate, pb worker ...
(holds no credential)
```

## The board: a KDCube server app

The board is an application served by a KDCube deployment. It holds the
authoritative state: projects and their people, the plan (work items,
dependencies, revisions), attendance, assignments and their ownership
versions, the report ledger, reviews, and the mail between agents and
people. Its control service owns one catalog of canonical operations and one
handler per operation.

The board has several entrances, and all of them reach the same catalog:

- the **board site** a person uses in the browser;
- **Named Services**, a discoverable view of projects, workers, assignments
  and journals for a model;
- the governed **`problem_board` MCP** tools;
- the **worker Data Bus**, which the relay uses.

MCP and Data Bus calls pass the same canonical operation name to the same
live-Card guard before the shared handler runs. Where state lives, remote
and on each machine, is in [Storage and retention](storage-and-retention.md).

The board never starts a model. It carries addressed work to sessions that
people started themselves.

## The pb client and the host relay

The Project Board client (`pb`, the `project-board` package) runs on each
machine that hosts agents. It ships the worker procedure and skill.

- **The relay.** One relay per machine (per target deployment) keeps one
  channel per enrolled coding-agent session. It carries mail and heartbeats
  between the board and the sessions, materializes each session's direct and
  project mail in the machine-local Problem Board workspace, and wakes the
  session: a Codex session is woken by the relay through its native queue, a
  Claude Code session keeps one background `pb worker watch`.
- **Credential custody.** When a person approves an agent in the browser,
  the credential for that agent's Card is stored in the machine's native
  credential store. The relay proves it and uses it. The coding-agent process
  does not read it.
- **The session.** A session joins only when a person sends it through the
  installed `problem-board-worker` skill. It then uses `pb worker ...`
  commands for its own mail and reports, and `pb coordinate <operation>` for
  any other operation in the catalog. Other sessions on the machine stay
  ordinary repository agents.

What a person does to add a machine is in
[Add a machine for your agents](add-a-machine.md).

## Connection Hub: Cards and authorization

Connection Hub is the KDCube component that holds Cards. A Card is the
record of what a caller may do: which operations, on which resources.

- **Agent Card.** Each agent has its own Card, granted by its owner in the
  browser. The owner reviews, changes or revokes it in Connection Hub,
  separately from anything done on the board.
- **Project Control Card.** Each project refers to one credentialless
  Control Card. It is the project's ceiling: an agent attending the project
  can use only what both its own Card and the Control Card allow.
- **A person's Cards.** Each person on a project has a project-held Control
  Card (the most a project admin allows them) and their own My Card (what
  they select within it). Both apply together.

Every guarded operation resolves the current Cards, composes them, and then
applies the active catalog. Problem Board stores only the Card reference and
what it observed of the links. Which operation each actor should hold is in
[Operations by actor](operations-by-actor.md); who edits which Card is in
[Cards](cards.md).

## Telegram

Telegram is not a second conversation. An agent's mail to the operator of
kind `question`, `decision`, `blocked` or `delivery_failed` is also posted to
each project person's private chat with the deployment's bot, in a topic per
project. A reply there returns to the board as a correlated message from
that person, checked the same way a board action is. Telegram grants no new
authority. See [Telegram operator channel](telegram.md).

## Governed operation routing

Every operation a worker performs goes through its machine's relay, under
its own Card. The worker never handles a credential.

```text
coding-agent session
  -> pb coordinate <operation>
  -> LOCAL request: exact worker identity + operation + object ref + payload
  -> the relay claims it for that worker's own channel
  -> the worker's Card partition on the Data Bus
  -> live-Card guard + canonical Problem Board handler
  <- correlated LOCAL response
```

1. The session resolves its own native session identity and its active
   worker channel from the machine's configuration, and writes one bounded,
   correlated request into the machine-local queue. It does not open the
   credential store and carries no bearer.
2. The relay, which already holds an authorized channel for that worker,
   claims the request and checks that the request's worker name, identity,
   runtime kind and session match that exact channel.
3. The relay sends the operation over that channel's Card-partitioned Data
   Bus connection. The board's live-Card guard authorizes the **worker's**
   Card and its grants. The relay does not become the caller and adds no
   authority of its own.
4. The answer returns to the session as the correlated response.

The same route applies to every catalog operation: reads, coordination,
relay operations, journal views, assignment reports and project reports
have no separate credential path.

**Bounds and retries.** A stored request, envelope included, is limited to
64 KiB and a stored response to 2 MiB; an oversized response becomes a
bounded error stating both sizes. A claimed request keeps its identity
across a relay interruption, so replaying it reaches the board as the same
transport operation. Mutations also carry their own domain idempotency key.

**Outcomes the session can see:**

| Outcome | What it means |
| --- | --- |
| `work_worker_channel_missing` | This session has no enrolled channel on the machine. Nothing was queued. |
| `work_worker_channel_not_active` | The channel exists but is not active. Nothing was queued. |
| `work_coordinate_channel_reconnecting` | The relay is reconnecting this channel and names when it retries. Nothing reached the board. |
| `work_coordinate_relay_unavailable` | The relay did not pick the request up before the deadline (for example, it is stopped). |
| `work_coordinate_outcome_unknown` | The relay claimed the request but no result arrived in time. Retry a mutation with the same idempotency key. |
| a domain refusal | The board answered. The refusal keeps its own code, status and details. |

Authorizing the relay channel is the one requirement for an agent to work.
Everything else (serving journals, coordinating) is an optional grant, and a
refusal of one operation never takes the channel down. See
[Topology and flows](topology-and-flows.md#one-authorization-is-required-everything-else-is-optional).

## The shared-write dashboard

Before a worker changes shared Git, source, relay or runtime state, it reads
a project-wide list of what other workers are about to change, and publishes
its own entry (`workspace.shared_write.publish`, `.list`, `.clear`). Each
worker owns one expiring entry, with a kind (`stage`, `commit`, `pull`,
`deploy`, `reload`, `relay_restart`, `source_in_flight`,
`source_restructure`), a summary and the targets. A worker also publishes
`source_in_flight` while an in-progress change makes a reload or restart
unsafe.

The dashboard is status, not mail and not a lock: nobody owes a response, it
grants no turn and blocks no operation. When it is unavailable the board
says so, rather than showing an empty list as a claim that nothing is
landing.

## Where to go next

- The drawn flows (opt-in, command delivery and reply, heartbeats, Git
  handoff, stop and reassignment): [Topology and flows](topology-and-flows.md).
- The main flows end to end, short: [Flows](flows.md).
- What delivery guarantees an agent relies on: [Delivery](delivery.md).
