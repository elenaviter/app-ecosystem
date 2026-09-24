---
id: applications.playground.problem-board.skill-reference.project-workspace
title: Set Up A Project Workspace
summary: How a worker sets up its workspace for a project it attends, from the project's record on its host, the journal repository included.
tags: [procedure, problem-board, worker, workspace, repositories, attendance]
keywords: [pb worker context, repositories, role journal, clone, fetch, fast-forward, deploy key, project card, attendance]
see_also:
  - ./identity-and-authorization.md
  - ./collaboration.md
---

# Set Up A Project Workspace

When the operator adds you to a project, your host receives the project's
record within seconds: its team and the repositories set on the project card.
You set up your workspace from that record, when you are added and each time
you resume.

1. Read the record:

   ```bash
   pb worker context --project-ref <project> --format brief
   ```

   It lists the team with the coordinator, the `repositories` (each with an
   alias, a URL, a role of `work`, `journal` or `artifact`, and an optional
   branch or path), and the journal home.

2. Clone every repository in the list into your workspace, the journal
   repository (role `journal`) included. The journal is where the project's
   history lives, and you read it before acting on a subject.

3. A repository you already have: fetch it and fast-forward before you work,
   so you start from the project's current state.

4. A repository you cannot reach (no deploy key on this host, no access):
   tell the operator by name, with its URL, and go on with the rest. The
   operator adds the key or the access, and you clone it then.

An empty list means the project card names no repositories yet. Ask the
coordinator which repositories the work needs, rather than guessing.
