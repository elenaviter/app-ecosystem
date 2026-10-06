Part of [coordinator](../coordinator.md).

## Stay reachable through every window

The coordinator's own notification path is armed at all times:

- **Before the watch expires:** re-arm it before or at its expiry notice, including during a runtime window. When the relay is down the watch reports nothing, and that is fine. What must not happen is a relay that comes back to a coordinator nobody can wake.
- **Every wait ends:** anything you wait for (a result file, a relay restart, a worker's reply) runs as a background check with a bounded end, so its completion or its timeout wakes you. A wait with no check behind it is time nobody accounts for.
- **After every window:** once the relay is back, receive immediately, before the all-clear, and verify each worker's wake was pushed (see the section above).

Why: on 2026-09-24 the coordinator's watch expired during a window and was not re-armed after the relay returned. Only an unrelated background check woke it. The operator: "if i did not write to you now that would stand still forever?"
