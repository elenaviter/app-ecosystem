Part of [coordinator](../coordinator.md).

## Recover a stalled Codex delivery

A running relay and an active Card prove transport, not that a model received
its mail. The relay gives a Codex wake one automatic retry. When the session
takes both without running `pb worker receive`, new mail joins the same wake
and nothing automatic submits again, on purpose: a session that ignored two
turns is not helped by an endless third. That state waits for you.

1. **Detect.** On the worker's host, `pb worker list` prints, for that worker,
   `NOTE: native delivery stalled: wake <id> taken without a receive and its
   one retry used since <time>; pending <n>; last inbox check <time>.` An idle
   Codex worker with nothing pending prints no such line.
2. **Diagnose.** Separate the layers before acting: the relay and the Card
   are transport; the note is the native queue; `last inbox check` is the
   model. A usage limit the provider enforces is a different state, read
   from the worker's usage with its reset, and a wake does not fix it. A wake
   the provider refused for usage is not the session ignoring it: once the
   refused limit's reset passes, the relay pushes that wake once more by
   itself, and the relay log says `wake re-armed ... reason=limit_ended`.
3. **Recover once.** Run the command the note prints, on that host:
   `pb worker wake-recover --worker <stable name> --wake-id <id>`. It
   submits the relay's own prompt for the existing wake and records the
   attempt: `submitted`, `failed` (nothing was queued; one more attempt is
   allowed) or `outcome_unknown`. A second call for the same wake is refused
   and shows the recorded attempt, because the native queue has no
   idempotency key and a repeat could hand the session two turns. While the
   relay holds the session for its limit, the command is refused
   (`field_worker_wake_recovery_held`, with `held_until`): recover after the
   reset, if the relay's own push has not reached it.
4. **Recheck the model, not the queue.** Queue admission is not handling.
   The recovery resolves only when the worker receives that wake: `pb worker
   list` then prints `last recovery: wake <id> resolved by the worker's
   receive at <time>`, and `last inbox check` moves. Read its replies and
   settlements for the work itself.
5. **Escalate.** When the recovery stays `submitted`, `outcome_unknown` or
   `reserved` (a call interrupted before its outcome was recorded) past a
   few minutes, or the session is not running, tell the operator in the
   project conversation (kind `blocked`), naming the worker, the wake, the
   recovery state and the pending count. Do not submit again. A recovery
   whose mail has since drained still shows, without a stall, until the
   worker's receive of that wake resolves it.

Why: on 2026-09-29 a Spark session sat for four hours with thirteen messages
behind one exhausted wake while its relay and Card read healthy; one native
prompt for that wake, sent by hand, recovered it (W405).
