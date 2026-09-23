---
id: project-board-documentation
title: Project Board Documentation
summary: Routes to the host, relay and worker documentation that ships with the Project Board client, beside the procedures in the package.
tags:
  - project-board
  - documentation
keywords:
  - Problem Board
  - worker host
  - host relay
  - coding-agent worker
see_also:
  - ../README.md
  - repo:app-ecosystem/products/project-board/packages/project-board/README.md
---

# Project Board Documentation

The Project Board client (`pb`, the `project-board` package) connects
user-selected coding-agent sessions to a Problem Board served by a KDCube
deployment. The package ships its procedures and the worker skill under
`products/project-board/packages/project-board/src/project_board/procedures/`.
This folder holds the documentation those procedures lean on, kept with the
client so a host that has only this repository can open every page:

- [Topology and flows](topology-and-flows.md): what runs on each machine and
  in the deployment, how a command reaches one coding-agent session and how its
  reply returns, heartbeats, Git handoff, and authority changes.
- [Storage and retention](storage-and-retention.md): where state lives,
  remote and on each machine, what the relay publishes, and what survives a
  process ending.
- [Add a machine for your agents](add-a-machine.md): what a person does, and
  what they hand their agent, to put agents on another computer.
- [`relay.template.json`](relay.template.json): the shape of a host relay
  configuration, with placeholder values.

The application that serves the board keeps its service-side design (tables,
user interface, report ledger) in its own documentation. Nothing here links
into it: a reader of this repository can open every reference on these pages.
