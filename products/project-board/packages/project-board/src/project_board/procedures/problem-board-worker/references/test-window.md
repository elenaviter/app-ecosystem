---
id: project-board.skill-reference.test-window
title: Pause For A Test Window
summary: Distinguish runtime-only exact-commit releases, host client quiescence and a global test freeze; readiness has different stop conditions in each.
tags: [procedure, problem-board, worker, test-window]
keywords: [test window, global test freeze, exact-commit release, ready, hold, paused, deploy, coordinator]
see_also:
  - ./runtime-actions.md
  - ./coordinator.md
---

# Pause For A Test Window

Read this when the coordinator announces a runtime action or relays a test
window. Today the requester of a test window is the operator, and it may be a QA
agent. The requester is a role and the protocol is the same.

The announcement says which kind of window it is, and they ask different
things of you. A restart of the machine itself is neither: it freezes every
agent on that machine until the operator resumes, and its READY means stop,
not keep working ([runtime actions](runtime-actions.md#a-machine-restart-freezes-every-agent-on-it)).
It takes precedence over the answers below.

## A runtime-only exact-commit release

A runtime-only reload or refresh loads the approved commit from a clean
release tree. Your own worktree is not that tree, so what you have in it,
committed or not, cannot change what loads. Answer `ready`, and keep working.
Answer `hold` with the condition that releases it only when one of these is
true of you:

- you can write the same filesystem tree the action will stage
- the approved candidate is meant to include a commit of yours that is not
  yet integrated onto the released ref
- the action would interrupt or conflict with a local or runtime operation
  you are running

These are the coordinator's three hold conditions ([coordinator](coordinator.md),
Reload, refresh, restart, step 2), and your answer applies the same ones.

## A host client switch or relay restart

Follow [Host Client Window Quiescence](runtime-actions.md#host-client-window-quiescence):
READY means calls drained and held before execution, not keep working until
a START wake arrives. The installer records acknowledged STARTING NOW or
evidenced idle/waiting state under that hold by the announced bounded deadline;
a non-quiesced participant stops the window. A successful START send proves
transport acceptance, not session handling. Every required send must succeed
and every affected session must satisfy the gate before the installer executes.

## A global test freeze

A requester who tests the running system as a whole asks for a global test
freeze by that name, and then every attending worker stops, because a worker
that keeps acting on the running system changes what the requester observes.

1. Finish the piece you are on. The piece, not the item.
2. If it cannot be committed as it stands, revert it rather than leave it in
   your worktree.
3. Commit it, so a long freeze loses nothing.
4. Tell the coordinator you are paused.
5. Stop. Do not start the next thing, and run nothing that changes the system
   under test: no runtime action, relay restart, or push to a ref the window
   releases. Answering mail is fine.
6. Wait for the coordinator to say the window has closed, then continue.

Only the installer the route names deploys (the coordinator when none is
named; [collaboration](collaboration.md) Rule 2). In a freeze it starts once every affected
worker that is available has reported paused. For a worker that is
unavailable, the coordinator first establishes from evidence that it has no
operation in flight the freeze would conflict with (its tree clean at its
last pushed commit, no runtime or relay call running, nothing writing to the
system under test) and records it as pending with that evidence. If the freeze
includes a host client switch or relay restart, the additional
[host quiescence gate](runtime-actions.md#host-client-window-quiescence) still applies.
Absence is
not quiescence: an unreachable or limited worker may still have an operation
running, and its absence neither pauses it nor holds the freeze by itself
([collaboration](collaboration.md) Rule 10). If finishing cleanly will take longer than the requester
would expect, say so, so they can decide whether to wait.
