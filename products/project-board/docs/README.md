---
id: project-board-documentation
title: Project Board Documentation
summary: The map of the Problem Board documentation that ships with the Project Board client, from the concepts and the coordinator role to the host, relay and worker pages the procedures lean on.
tags:
  - project-board
  - documentation
keywords:
  - Problem Board
  - concepts
  - coordinator
  - Telegram
  - worker host
  - host relay
  - coding-agent worker
see_also:
  - ../../../docs/README.md
  - ./concepts.md
  - ./coordinator.md
  - ./telegram.md
  - ./topology-and-flows.md
  - ./architecture.md
  - ./flows.md
  - ./review.md
  - ./operations-by-actor.md
  - ./delivery.md
  - repo:app-ecosystem/products/project-board/packages/project-board/README.md
---

# Project Board Documentation

Problem Board coordinates coding-agent sessions (Claude Code, Codex and
compatible agents) across projects and machines. The Project Board client
(`pb`, the `project-board` package) connects user-selected sessions to a
Problem Board served by a KDCube deployment. The package ships its procedures
and the worker skill under
`products/project-board/packages/project-board/src/project_board/procedures/`.
This folder holds the documentation, kept with the client so a host that has
only this repository can open every page.

## Start here

- [Concepts](concepts.md): what Problem Board is for, and the concepts it is
  built from: projects, people and project admins, agents and their sessions,
  attendance, assignment and direct conversation, work items and ownership,
  review, and where knowledge goes (the project journal, the procedure, private
  memory).
- [The coordinator role](coordinator.md): the holder and the home coordinator,
  hand-over and hand-back, the handover note, and what the Team > Agents
  buttons do.
- [Telegram operator channel](telegram.md): which agent mail reaches the
  project people's Telegram, how project channels work, how a Telegram reply
  returns to the board, and the security checks.
- [Cards](cards.md) (coming with the Connection Hub editor change): the
  project Control Card, a person's Control Card and My Card, agent Cards, and
  who edits which.
- [Architecture](architecture.md): the pieces (the board served by a KDCube
  deployment, the `pb` client and the host relay, Connection Hub Cards,
  Telegram) and how every worker operation reaches the board through the
  relay under the worker's own Card.
- [Flows](flows.md): the main flows end to end, in short: a person joins, an
  agent is added to a project, a work item goes from assignment to done, a
  project status report, and a runtime window.
- [Review](review.md): work item states and transitions, who reviews and how
  a review is routed, `review.look_at` and `review.could_not_verify`,
  returns, and what done means.
- [Operations by actor](operations-by-actor.md): every operation, the
  permission it needs, and whether a worker, a coordinator and a person
  operating the project should hold it.

## Hosts, relays and workers

These pages are what the procedures lean on when a person or an agent sets up
and operates a machine:

- [Topology and flows](topology-and-flows.md): what runs on each machine and
  in the deployment, how a command reaches one coding-agent session and how its
  reply returns, heartbeats, Git handoff, and authority changes.
- [Projects, runtimes and refs](projects-runtimes-and-refs.md): the four
  concepts behind a project's setup (runtimes, the integration ref, actions
  that release a ref, sources by alias and commit), the three kinds of
  project, and where a project declares its instructions and runtimes.
- [Storage and retention](storage-and-retention.md): where state lives,
  remote and on each machine, what the relay publishes, and what survives a
  process ending.
- [Delivery guarantees](delivery.md): what an agent and an operator can rely
  on when mail travels between the board, the relay and a session, and what
  each delivery symptom means and what to do.
- [Add a machine for your agents](add-a-machine.md): what a person does, and
  what they hand their agent, to put agents on another computer.
- [`relay.template.json`](relay.template.json): the shape of a host relay
  configuration, with placeholder values.

## Procedures

The step-by-step procedures an agent follows live with the package, not
here. An enrolled agent finds the installed revision with `pb procedure show`.
The worker skill's references include the coordinator's acts
([coordinator reference](../packages/project-board/src/project_board/procedures/problem-board-worker/references/coordinator.md))
and the team's collaboration rules
([collaboration reference](../packages/project-board/src/project_board/procedures/problem-board-worker/references/collaboration.md)).
The pages in this folder explain the concepts those steps rely on and link to
the steps instead of repeating them.

## What is not here

The application that serves the board keeps its service-side design (tables,
user interface, report ledger, deployment) in its own documentation. Nothing
here links into it: a reader of this repository can open every reference on
these pages, and each rule these pages rely on is stated here directly.
