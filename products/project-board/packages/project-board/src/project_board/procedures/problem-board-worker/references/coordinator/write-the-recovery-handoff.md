Part of [coordinator](../coordinator.md).

## Write the recovery handoff

A recovery summary (the one a runtime writes at a compaction, or one you
write for a successor session) carries the state still open, in at most
1,500 words, and refs to everything else (operator, 2026-10-05, W563 Q6).
Completed work is on the board and in the journal; the handoff names it by
ref, never retells it. Write it under these headings, `none` when empty:

1. **Operator constraints in force**: each quoted verbatim with its ref.
2. **Open leases and unknown outcomes**: `message_ref`, `lease_id`,
   correlation, outbox ids and mutations whose outcome is not known yet.
3. **Windows**: an active runtime window, its approved scope, step, rollback
   point, readies and holds.
4. **Responsibilities**: per item key, the current phase, the next actor and
   the checkpoint time; for each change, merged or deployed, by its evidence.
5. **Waits**: who waits on whom, and who clears it.
6. **Loaded procedure**: the installed revision you loaded, and the
   coordinator modules you have read under it.
7. **Locators**: the exact commands and refs you will reuse (a payload
   shape, a project ref), so no help or contract discovery is repeated.

No transcript and no history: what is stable stays behind its ref.

A longer handoff names the open item that needs the room and why, and stays
bounded. On resume, reuse the loaded revision when `pb procedure verify`
names the same one, receive, and act on the handoff; reread no history the
handoff names by ref unless a decision needs it.
