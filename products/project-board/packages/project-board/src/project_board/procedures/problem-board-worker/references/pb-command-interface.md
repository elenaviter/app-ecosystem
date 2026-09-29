---
id: project-board.skill-reference.pb-command-interface
title: Use Problem Board Through The Worker CLI
summary: The positive worker path from a task or refusal to a canonical PB operation, payload, ref and recovery.
tags: [procedure, problem-board, worker, cli, operations, recovery]
keywords: [pb worker, pb coordinate, project.plan.search, project.plan.item, plan.notes.list, plan.item.update, canonical operation, refusal]
see_also:
  - ./brief-output.md
  - ./delivery-and-recovery.md
  - ./project-workspace.md
---

# Use Problem Board Through The Worker CLI

Open this before the first governed project operation in a session and after
an operation is refused. The installed `pb` command is the worker's Problem
Board interface.

## The two command groups

`pb worker ...` owns this native session's enrollment and Card connection,
project context and workspace, inbox leases and settlement, mail, assignment
reports, journal, availability and visible worker information. Start with `pb
worker --help`, then the selected subcommand's `--help`.

`pb coordinate <operation-id> --object-ref <project-ref> ...` invokes one
canonical governed project operation through this session's persistent Card
relay. `pb coordinate --help` gives the transport arguments. The relevant
procedure's worked command and payload give the operation-specific fields; the
published [Operations by actor](repo:app-ecosystem/products/project-board/docs/operations-by-actor.md)
identifies the canonical operation and required authority. An operation keeps
the same canonical ID wherever the public documentation names it.

Use the project and worker refs printed by `pb worker context`. Copy every
other ref whole from the command that returned it. A later operation receives
that printed ref; it is never reconstructed from an alias, key or title.

## Find and read work

1. Search for the subject with `project.plan.search` so an existing item is
   reused instead of duplicated.
2. Read one current item with `project.plan.item` and its `item_key`:

   ```bash
   pb coordinate project.plan.item \
     --object-ref <project-ref> \
     --payload-json '{"item_key":"<Wn>"}'
   ```

3. Copy its returned `identity_ref`, versioned item ref and revision. Read its
   notes with `plan.notes.list` when the task or route depends on them.
4. Update it with `plan.item.update`, using that returned ref and revision as
   `work_ref` and `expected_revision`. The complete payload is in the main
   skill under Work, Report, And Journal.

Use the same pattern for another operation: locate its worked procedure,
copy the canonical operation ID and required payload fields, obtain every ref
from a current PB read, then call `pb coordinate` through the Card relay.

## Handle a refusal or uncertain outcome

Read the returned code, message and fields as the result. Follow the recovery
named by that output, the operation's procedure or [delivery and recovery](delivery-and-recovery.md).
A known refusal changes only the field or authority the refusal names. An
outcome-unknown write is retried unchanged under the same `idempotency_key`;
the outbox or receipt check comes first when the command prints one. A missing
or inactive Card channel, unavailable relay, outcome-unknown response and Card
grant denial are separate cases and keep their separate recoveries.
