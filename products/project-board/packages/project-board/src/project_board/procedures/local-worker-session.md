---
id: app-ecosystem.project-board.procedure.local-worker-session
title: Run A Problem Board Local Worker Session
summary: Routes a selected Claude Code or Codex session to the versioned worker skill package that owns enrollment, addressed mail, settlement, reporting, journaling, recovery, and detach.
tags: [procedure, local-worker, codex, claude-code]
keywords: [worker skill, worker listen, worker receive, lease settlement, native session identity]
see_also:
  - repo:applications/playground/domain-solution/apps/problem-board@1-0/local/AGENTS.md
  - repo:applications/playground/domain-solution/docs/worker-mail-protocol.md
---

# Run A Problem Board Local Worker Session

Use the installed `problem-board-worker` skill inside the exact user-selected
Claude Code or Codex session. It owns the executable selected-session flow and
conditionally routes to its focused references. `pb procedure show` reports
the exact installed revision and source root.

The public [worker/operator router](agent-worker.md) chooses between this role
and machine administration. Host setup, relay lifecycle, filesystem policy,
Connection Hub consent, attendance, assignment, suspension, and revocation are
operator-owned and remain outside the worker skill.

Inspect, install, and verify the same versioned package through:

```bash
pb procedure show
pb procedure install --target codex --target claude-code
pb procedure verify
```

A running session explicitly re-reads its installed
`problem-board-worker/SKILL.md` after an upgrade; package installation updates
future reads and does not rewrite a model context already in progress.
