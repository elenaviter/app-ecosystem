Part of [collaboration](../collaboration.md).

## Rule 10. A runtime window speaks one channel that survives it

While the platform is down for an apply or a migration, the board is
unreachable, so nothing said during the window reaches a worker on another
machine. The announcement before the window therefore names each step's owner,
the rollback trigger, and the point after which nothing is expected from
remote workers. The all-clear on the board is the only resume signal, and
every result produced during the window is posted to the board after it. A
file on the host that ran the window is not a channel: on 2026-09-23 the apply
result came through one, and a worker on another machine could not read it
(P14, four yes votes). Readiness for a window is explicit: each affected
owner that is available and active answers READY, or HOLD naming its
in-flight operation and when it ends, and silence is not READY. An affected
participant who is unavailable is recorded as pending with its reason; it
holds nothing that does not depend on it (a hold names its evidence, Rule 16). Absence is not quiescence: before
the window runs, its owner establishes from evidence whether that participant
has an operation in flight the window would conflict with (the conditions in
the coordinator reference). For a runtime-only exact-commit release, only
such an operation holds it. A host client switch or relay restart additionally
requires [Host Client Window Quiescence](../runtime-actions.md#host-client-window-quiescence),
which holds future calls before execution; neither a queued START wake nor a
missing answer waives it. That gate waits only for the initiating team's
available agents, and tells the operator on Telegram about the other agents
on the machine (the same section).
The test-window reference owns the pause procedure itself, this rule owns
what the window says to the team.
