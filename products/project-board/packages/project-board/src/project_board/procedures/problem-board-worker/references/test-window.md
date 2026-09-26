---
id: project-board.skill-reference.test-window
title: Pause For A Test Window
summary: What a worker does when the coordinator relays a test window: converge to a clean committed tree, report it, stop, and wait for the window to close.
tags: [procedure, problem-board, worker, test-window]
keywords: [test window, clean tree, paused, deploy, coordinator]
see_also:
  - ./runtime-actions.md
---

# Pause For A Test Window

Read this when the coordinator relays a test window. Today the requester is
the operator; it may be a QA agent. The requester is a role and the protocol
is the same.

1. Finish the piece you are on. The piece, not the item.
2. If it cannot be committed as it stands, revert it rather than leave it in
   the tree.
3. Commit it.
4. Tell the coordinator your tree is clean and you are paused.
5. Stop. Do not start the next thing: a worker that commits and immediately
   begins something else has passed through a clean state rather than
   converged, and the deploy lands in the middle of the next change.
6. Wait for the coordinator to say the window has closed, then continue.

Only the coordinator deploys, and only once every worker has reported clean.
If finishing cleanly will take longer than the requester would expect, say so,
so they can decide whether to wait.
