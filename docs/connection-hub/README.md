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
  - ./quick-start-local.md
  - ./local-client-helper.md
  - ./configuration-and-capabilities.md
  - ./connection-hub-architecture.md
  - ./testing/end-to-end-acceptance.md
  - ./package/extraction-architecture.md
  - ./frontend/README.md
  - ./service/README.md
  - ./recipes/direct-protected-service.md
  - https://github.com/kdcube/kdcube/blob/main/app/ai-app/docs/service/cicd/delegated-management-service-README.md
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
- **[`testing/`](testing/end-to-end-acceptance.md)** - the complete human-runnable
  acceptance procedure for cards, accounts, managed MCP, named services, live
  consent, Claude Code, revocation, and durability.
- **[Local MCP client helper](local-client-helper.md)** - KDCube host selection,
  macOS Keychain custody, one stdio bridge, and verified registration paths for
  Claude Code, Claude Desktop, Hermes, OpenClaw, and other stdio clients.

For the shortest working path from an empty machine to an external MCP client,
use [Run Connection Hub Locally With KDCube](quick-start-local.md).

KDCube supplies the first runtime host for the package and application. The
standalone service remains planned.

KDCube's
[delegated management service](https://github.com/kdcube/kdcube/blob/main/app/ai-app/docs/service/cicd/delegated-management-service-README.md)
is the reference state-changing integration. It uses live direct admission,
signed request-bound browser approval, and a provider-side effect ledger to
reload one exact application without repeating an accepted effect.

The package contracts are:

- [Configuration and capabilities overview](configuration-and-capabilities.md)
- [Run Connection Hub locally with KDCube](quick-start-local.md)
- [Connect local MCP clients without bearer files](local-client-helper.md)
- [Connection Hub architecture and semantic requirements](connection-hub-architecture.md)
- [Delegated authority and admission](package/delegated-authority-and-admission.md)
- [Delegated access cards](package/delegated-cards.md)
- [OAuth delegated credential protocol](package/oauth-delegated-credential-protocol.md)
- [Connection Hub and governed MCP end-to-end acceptance](testing/end-to-end-acceptance.md)
- [Protect an external backend with Connection Hub](recipes/direct-protected-service.md)
