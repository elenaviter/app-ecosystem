---
id: connection-hub-documentation
title: Connection Hub Documentation
summary: Maps the Connection Hub Python package, KDCube application, authority architecture, integration recipes, and planned standalone host.
tags:
  - connection-hub
  - documentation
keywords:
  - delegated access authority
  - Connection Hub
  - identity cards
see_also:
  - ./configuration-and-capabilities.md
  - ./connection-hub-architecture.md
  - ./package/extraction-architecture.md
  - ./frontend/README.md
  - ./service/README.md
  - ./recipes/direct-protected-service.md
---

# Connection Hub documentation

Connection Hub is the product through which users manage connected accounts
and delegated access. Its Python package provides the portable authority and
client contracts: identity cards for agents and automations, one central record
of authority, and per-call resolution at the service boundary. Start with the
canonical
[Connection Hub architecture](connection-hub-architecture.md) for the semantic
model, storage authorities, surfaces, and host boundaries.

This folder documents the product's current package and application plus its
planned standalone host:

- **[`package/`](package/extraction-architecture.md)** - the Python package
  (`connection-hub` on PyPI): the authority domain, boundary and client SDK, actor
  and grant reference format, admission, structured denials, and claim
  publication. The extraction architecture records what the package owns and
  what a host supplies.
- **[`frontend/`](frontend/README.md)** - the live Connection Hub application,
  where a user connects accounts,
  issues and edits identity cards, and watches what the callers did.
- **[`service/`](service/README.md)** - the current KDCube-hosted Connection
  Hub service boundary and its future standalone wrapper boundary.
- **[`recipes/`](recipes/direct-protected-service.md)** - runnable integration
  paths, beginning with an external backend that asks Connection Hub for a
  live delegated operation decision.

KDCube supplies the first runtime host for the package and application. The
standalone service remains planned.

The package contracts are:

- [Configuration and capabilities overview](configuration-and-capabilities.md)
- [Connection Hub architecture and semantic requirements](connection-hub-architecture.md)
- [Delegated authority and admission](package/delegated-authority-and-admission.md)
- [Delegated access cards](package/delegated-cards.md)
- [OAuth delegated credential protocol](package/oauth-delegated-credential-protocol.md)
- [Protect an external backend with Connection Hub](recipes/direct-protected-service.md)
