---
id: project-board-cards
title: Cards And Who Edits Them
summary: Connection Hub Cards in a Problem Board project, and who edits which Card; the full page follows the change that makes Connection Hub the only Card editor.
status: pending
tags:
  - project-board
  - cards
  - connection-hub
keywords:
  - Control Card
  - My Card
  - project admin
  - Connection Hub
see_also:
  - ./README.md
  - ./concepts.md
---

# Cards And Who Edits Them

> **Pending.** This page is completed when the change that makes Connection
> Hub the only Card editor is released (the Connection Hub editor for project
> Cards, operation groups from the service catalog, and the board's links
> replacing its own Card editor). Until then it states only the rules already
> decided.

## The rules

- **Every Card is edited in Connection Hub, and only there.** Problem Board
  links to a Card, optionally with a preselection or a focus for the purpose
  the link serves. It builds no Card editor of its own and keeps no stored
  copy of the decision. The operator's ruling (2026-09-26): "it must be cards
  in the connection hub ... please do not build new interface for this, this
  is simply wrong! and does not scale."
- **How operations are grouped** (Review, Work, Plan, People) is declared with
  the operations in the service catalog, so Connection Hub groups them for
  every client.
- **Who edits which Card in a project.** A project admin (the Problem Board
  project's admin role, not a KDCube role) edits every Card the project holds:
  the project Control Card, each person's Control Card, their own included,
  and each agent's project Card. A member reads their own Control Card and
  edits only their own My Card, within their Control Card.
- **The project Control Card caps every agent Card on the project** (AND), and
  a Card Refresh is capped by it too: after new operations reach the catalog,
  a project admin ticks them on the project Control Card first.
