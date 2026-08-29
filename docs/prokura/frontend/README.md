---
id: prokura-frontend
title: Prokura Frontend
summary: Documents the Connection Hub application that lets users connect accounts and manage delegated identity cards backed by Prokura.
tags:
  - prokura
  - frontend
keywords:
  - Connection Hub
  - delegated identity cards
  - connected accounts
see_also:
  - ../README.md
  - ../package/extraction-architecture.md
---

# Prokura frontend

The Connection Hub application is Prokura's KDCube-hosted frontend. It lets a
user connect accounts, issue and edit delegated identity cards, inspect
current authority, and respond to a caller's structured denial with the exact
grant change that resolves it.

The application source lives at
[`apps/connection-hub@1-0`](../../../apps/connection-hub@1-0). A KDCube
deployment loads it by repository, ref, and subdirectory, while the app uses
KDCube host adapters over the Prokura authority package.

The detailed application contract is documented under
[`application/`](application/README.md).
