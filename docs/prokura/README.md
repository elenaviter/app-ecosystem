---
id: prokura-documentation
title: Prokura Documentation
summary: Maps the Prokura authority package, the KDCube-hosted Connection Hub application, its architecture and interfaces, and the planned standalone host.
tags:
  - prokura
  - documentation
keywords:
  - delegated access authority
  - Connection Hub
  - identity cards
see_also:
  - ./connection-hub-architecture.md
  - ./package/extraction-architecture.md
  - ./frontend/README.md
---

# Prokura documentation

Prokura is the delegated-access authority: identity cards for agents and
automations, one central record of authority, and per-call resolution at the
service boundary. Start with the canonical
[Connection Hub architecture](connection-hub-architecture.md) for the semantic
model, storage authorities, surfaces, and host boundaries.

This folder documents two current forms and one planned host:

- **[`package/`](package/extraction-architecture.md)** - the Python package
  (`prokura` on PyPI): the authority domain, boundary and client SDK, actor
  and grant reference format, admission, structured denials, and claim
  publication. The extraction architecture records what the package owns and
  what a host supplies.
- **[`frontend/`](frontend/README.md)** - the Connection Hub application: the
  live frontend built on top of the package, where a user connects accounts,
  issues and edits identity cards, and watches what the callers did.
- **`service/`** - a future standalone host of the same package and app
  contracts. It is not implemented yet.

KDCube supplies the first runtime host for the package and frontend. The
standalone service remains planned.

The package contracts are:

- [Connection Hub architecture and semantic requirements](connection-hub-architecture.md)
- [Delegated authority and admission](package/delegated-authority-and-admission.md)
- [Delegated access cards](package/delegated-cards.md)
- [OAuth delegated credential protocol](package/oauth-delegated-credential-protocol.md)
