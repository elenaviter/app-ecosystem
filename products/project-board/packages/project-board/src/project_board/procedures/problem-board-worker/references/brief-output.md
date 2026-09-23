---
id: applications.playground.problem-board.skill-reference.brief-output
title: Read Brief Output
summary: How a worker reads a pb receipt, an ERROR and a ref in brief output, and what it does after an outcome-unknown response.
tags: [procedure, problem-board, worker, brief-output, receipt, idempotency]
keywords: [format brief, state applied, state refused, replayed, observed_revision, ERROR, outcome unknown, idempotency_key, same key, fresh key, ref, UNREADABLE, pb render]
see_also:
  - ./delivery-and-recovery.md
  - ./collaboration.md
---

# Read Brief Output

Open this before any retry of a command that returned `ERROR`, when a receipt
is unclear, or when a ref has to be copied.

## The shape

Every `pb` command accepts `--format brief` anywhere on the line, and
`PB_FORMAT=brief` makes it the session default. Brief output is complete text:
`OK` or `ERROR <code>` first, every ref, id and key whole on its own line,
bodies in full, and each follow-up command (`lease-read`, `settle`, and for a
question or request the correlated `send`) printed complete with the refs and
this session's runtime flags. `pb render --file <path>` renders saved output
the same way.

## A governed mutation's receipt

A `pb coordinate` receipt names its outcome in `state`, and that is the field
to read.

- `state: applied`, with `(replayed)` after it when the service replayed an
  earlier attempt under the same `idempotency_key`. The receipt is the original
  one, and nothing was written twice.
- `state: refused`, with `reason` and `summary`, and where the service gave one
  an `error.code`, `error.message` and `error.details`.
- `observed_revision` is in every receipt, applied or refused. It says which
  revision the service saw, not that anything conflicted. An empty `error` slot
  is not printed.

Why this is written down: on 2026-09-23 a caller read `error = {}` printed
beside `observed_revision` on fourteen successful `plan.note.append` receipts
as fourteen conflicts and retried each under a fresh `idempotency_key`, writing
fourteen notes (W276). The renderer now prints `state` first and drops the
empty slot, and the rule stands on its own: the outcome is in `state`, not in
another field's presence.

## `ERROR <code>` is not a refusal, and its code decides the retry

An `ERROR` envelope carries no receipt. It carries a code, and the code says
which of three things happened. Read it before any retry.

- The write never reached the service, and the error proves it. A coordinate
  request cancelled before any relay claimed it exits as
  `work_coordinate_relay_unavailable`, after the client has checked that no
  claim happened, and a channel still reconnecting refuses with
  `work_coordinate_channel_reconnecting` before anything is sent. The outcome
  is known, nothing applied. Follow the reason (let the channel reopen, read
  `pb worker inspect`), then send again. That is a new decision, not a replay.
- The request was refused before any write, on its shape or its admission:
  `work_value_required`, a `field_*_invalid` code, an authorization refusal.
  Nothing applied. Fix what the code names. Repeating the same request repeats
  the refusal.
- The outcome is unknown: the request may have applied and its response been
  lost, or it may never have arrived. `work_coordinate_outcome_unknown` names
  this class on the `pb coordinate` path (the relay claimed the request and no
  correlated result arrived by the deadline), `data_bus_outcome_unknown` names
  it on the bus, and a deadline is the usual way into it. The recovery is the same
  request under the same `idempotency_key`. That is safe whatever happened: if
  the write applied, the replay returns the original receipt marked
  `(replayed)` and writes nothing, and if it did not, it applies once. A fresh
  key is a second write, and after an unknown outcome it is the one thing that
  can double the effect.

The envelope's shape alone cannot tell these apart, so the reader does not
classify by `ERROR`. It classifies by the code, and a code it does not know is
treated as outcome unknown until the reference or the operation's own text
says otherwise.

An operation whose recovery is more than a repeat says so in its own error. A
deadline on `pb worker report` exits nonzero as
`field_assignment_report_outcome_unknown`, names the outbox ID and prints
`pb worker outbox-status --outbox-id <id>`: read that row first, then retry the
same report unchanged (the skill's Work, Report, And Journal section). Changed
content or an invented source event is a different report, not recovery.

## Refs are copied, never assembled

Do not write a JSON reader for `pb` output: hand-written readers exited
silently on unexpected shapes and truncated copied refs. Brief output cannot be
silent. An error envelope renders as `ERROR`, and text that is not an envelope
renders as `UNREADABLE` followed by the text itself, exit code 2. A ref is
copied as a whole line or obtained from a rendered command, never assembled,
never taken from a wrapped fragment, and never composed from a key and a
title: `project.plan.item` prints the `identity_ref` to copy.
