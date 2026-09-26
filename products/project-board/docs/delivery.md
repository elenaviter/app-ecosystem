---
id: project-board-delivery
title: Delivery Guarantees
summary: What an agent and an operator can rely on when mail and assignments travel between the board, a machine's relay and a coding-agent session, the separate states a message passes through, and what a delivery symptom means and what to do about it.
tags:
  - project-board
  - delivery
  - mail
  - relay
  - reliability
keywords:
  - delivery invariants
  - transactional receive
  - lease
  - settle
  - quarantine
  - wake
  - delivery_failed
  - reachability
  - failure matrix
see_also:
  - ./README.md
  - ./architecture.md
  - ./topology-and-flows.md
  - ./storage-and-retention.md
  - ./telegram.md
---

# Delivery Guarantees

Mail and assignments travel from the board, through a machine's relay, into
a coding-agent session, and back. This page states what an agent and an
operator can rely on along that route, and what to do when something looks
wrong. The route itself is drawn in
[Topology and flows](topology-and-flows.md#command-delivery-and-reply). The
commands an agent runs are in the
[delivery and recovery reference](../packages/project-board/src/project_board/procedures/problem-board-worker/references/delivery-and-recovery.md).

## A message has several separate states

```text
sender's machine accepted it
  -> the board admitted it to the recipient's durable queue
  -> the recipient's relay materialized it on that machine
  -> the session was notified
  -> pb worker receive returned the complete body and a lease to the model
  -> the agent sent any required visible reply
  -> pb worker settle recorded it acknowledged or refused
```

No earlier state claims a later one. A relay that is online proves a
transport process ran, not that a model saw a body. A notification says mail
exists; receive returns it; settlement records that it was handled.

## Delivery invariants

**One message cannot block the rest.**

- Message content does not control transport health. A malformed message
  fails, or is set aside, as one item. It cannot stop the relay, the mailbox
  or later messages.
- A message that cannot be built is retried on its own. After three failed
  attempts it moves to an inspectable quarantine while healthy mail
  continues.
- Context that does not resolve is a warning, not a failure. An unknown work
  item reference arrives beside the intact message as a delivery warning.
- If one referenced item cannot be read, the others are still delivered. The
  unread ones are named and stay eligible for retry.

**Receive is all or nothing, and bounded.**

- A receive returns a complete batch with exact leases, or returns every
  provisional lease to the inbox before reporting failure.
- One size budget covers a receive. A message that would cross it stays
  pending and unleased for the next receive.
- A session can always re-read what it already holds: it can page every
  lease it holds and re-read any one of them without waiting for expiry.
- Attached files are part of the message, each with an exact read command
  that checks the lease, size and content hash.

**Nothing is lost silently.**

- The sender gets a result. A rejected message produces one
  `delivery_failed` notice to the sender, naming the message, the recipient,
  the field, the code and the reason. The recipient keeps listening.
- Only stable worker names are addresses. A display alias, an unknown worker
  or a retired worker is refused before any mailbox is created, and the
  board records the refusal.
- Refused mail stays recoverable. The sender keeps the complete message and
  can replay it, and only the sender, to a corrected stable address.
- Retiring a worker and its pending mail change together: its pending
  delivery is withdrawn, the refusal recorded, and its senders told. Nothing
  is left in a mailbox nobody will read.
- A lease that expires unsettled returns the message for redelivery, marked
  with the prior handling so the work is not repeated.

**Wakes are exact.**

- A wake stays outstanding until a receive acknowledges it. At most one
  native wake per session is queued; acknowledged, stale and duplicate wakes
  are removed before another is added.
- Mail already on a machine is announced to its session even when the
  machine cannot reach the board at that moment.

**Handling is the agent's to record.**

- Receiving is not handling. Only settlement records whether a message was
  acknowledged or refused.
- A lease is held for at most an hour, renewals included. An agent settles
  once the outcome is recorded where it lasts (a reply, a report, a note on
  the item), not when the long work behind it finishes.

**Assignments recover once.**

- A durable assignment is delivered to its worker once. If it cannot be
  delivered, the worker gets one failure message with the assignment and the
  reason, and it is not retried until the assignment itself changes.
- Retries are recognised by identity, not by rendered text: the same
  assignment at the same ownership version, or the same mail under the same
  idempotency key from the same sender, is the same item.

**Reachability is evidence.** Relay connection, session attachment, inbox
check, a non-empty receive and settlement are separate timestamps, and the
board shows them separately.

## What the board does not do

The board keeps a durable route to a coding-agent session that is already
running. It does not start or resume a stopped session, and it cannot force
a stopped process to take a turn. So it presents receipt and settlement as
evidence, never infers them from registration or relay liveness. That does
not permit loss: mail stays pending, wake retries stay bounded and visible,
expired leases come back, and malformed items are reported or quarantined.

The board does not interrupt an edit in progress. A notification surfaces
when the session's current step yields; during long work an agent receives
at each coherent boundary. A correction that must hold across an unread
inbox, a resume or a reassignment is written into the work item, not only
sent as mail.

## Failure matrix

| Symptom | What it means | What to do |
| --- | --- | --- |
| A received message carries a delivery warning about an unknown work item | The message arrived intact; only its item reference did not resolve. The sender got a `delivery_failed` notice. | Handle the message. Ask the sender which item it means if that matters. |
| The receive reports quarantined messages | A message could not be built after repeated attempts and was set aside. Healthy mail continued. The sender was told. | List and read the quarantined message, then release it for a fresh attempt or discard it with a reason. It has no lease to settle. |
| A receive fails | The whole batch was rolled back; nothing is half-leased. | Receive again. |
| A receive looks partial or truncated | Some leases may be held without their body in view. | Stop changing things, page your held leases and re-read each one before continuing. |
| A message comes back with prior handling | Its lease expired before it was settled. | Settle the new lease without repeating the work, unless the prior record says the effect was incomplete. |
| Some referenced items were not delivered | Those items could not be read; the rest arrived with leases. | Handle what arrived. The named items retry once storage recovers. |
| A send is refused as `work_worker_not_found` | The address is not a stable worker name (an alias, an unknown or a retired worker). | Correct the recipient to the stable name and replay from your outbox with the same idempotency key. |
| A send or call is refused as reconnecting | The relay is reconnecting this channel. Nothing reached the board. | Wait until the named next attempt, then retry with the same idempotency key. |
| `work_coordinate_relay_unavailable` | The relay on this machine did not pick the request up (for example, it is stopped). | Check the relay's status on the machine. A relay restart is agreed with the other agents on the host first. |
| `work_coordinate_outcome_unknown` | The relay took the request, but no result arrived in time. | Read the board's state first; retry a mutation only with the same idempotency key. |
| A domain refusal (a conflict, a missing grant) | The board answered. The route works. | Fix the cause the refusal names. Do not restart or re-authorize anything. |
| The channel needs re-authorization | The relay cannot use the worker's Card. The agent can do nothing until it is fixed. Mail stays pending. | The agent's owner re-approves the Card. Retrying does not help. |
| The same Codex wake arrives twice | A route-integrity fault: duplicates should have been removed. | Run the exact receive it names, do not repeat completed side effects, and report the wake's origin and attempt. |
| A Codex card reads "waiting for wake" | Normal between wakes: its relay reports, and no mail is pending or its wake is queued. | Nothing. |
| A Codex card reads "wake relay stale" | The host relay that owns the Codex queue stopped reporting, so no wake can reach the session. | Check the relay on that host; the session itself may be fine. |
| A worker shows `not_listening` or an overdue inbox check | Its recent inbox checks are missing. For Claude Code its watch has stopped; an idle Codex session may still be reachable through its queue. Mail stays queued. | For Claude Code, re-arm the watch and receive. Diagnose a specific failed delivery from route and wake evidence, not this label alone. |
| The board says the relay is online, but the agent does not answer | The relay runs, but the session may be mid-turn, idle and not woken, or closed. | Report each stage separately (admitted, materialized, wake outstanding, received, replied, settled). When only a person can act, tell them which worker, what it holds, and since when. |

When a failure is unclear, name the layer first: which one answered and
which one failed. The error code usually says. Compare with a working peer
at the same time: if its calls succeed, the fault is this worker's channel;
if they fail too, it is the relay or the board. The layer-by-layer steps are
in the
[delivery and recovery reference](../packages/project-board/src/project_board/procedures/problem-board-worker/references/delivery-and-recovery.md#diagnosing-a-failure-layer-by-layer).
