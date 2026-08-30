---
id: connection-hub-frontend
title: Connection Hub Application
summary: Documents the Connection Hub application through which users connect accounts, manage delegated cards, and authorize managed or direct protected-service operations backed by Connection Hub.
tags:
  - connection-hub
  - frontend
keywords:
  - Connection Hub
  - delegated identity cards
  - connected accounts
see_also:
  - ../README.md
  - ../connection-hub-architecture.md
  - ../package/extraction-architecture.md
---

# Connection Hub application

Connection Hub is the product application. Its current KDCube-hosted form uses
Connection Hub as the portable delegated-authority engine and client SDK. It lets a
user connect accounts, issue and edit delegated identity cards, inspect current
authority, and respond to a caller's structured denial with the exact grant
change that resolves it.

The [Connection Hub architecture](../connection-hub-architecture.md) defines
the semantic model, storage authorities, surface inventory, and direct
protected-service admission boundary.

The application source lives at
[`apps/connection-hub@1-0`](../../../apps/connection-hub@1-0). A KDCube
deployment loads it by repository, ref, and subdirectory, while the app uses
KDCube host adapters over the Connection Hub authority package.

The detailed application contract is documented under
[`application/`](application/README.md).
