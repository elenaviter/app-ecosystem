Part of [coordinator](../coordinator.md).

## Hand the coordinator role over, and take it back

The role is held by one agent at a time, the holder. The home coordinator keeps
its label while another agent acts. Mail for whoever coordinates goes to
`--recipient coordinator` with `--project-ref`: the board resolves it to the
holder at send time, so procedure text and teammates address the role, never
the agent holding it today.

**When to hand over:** your runtime reports a limit coming (`pb worker
limit-state`, W26), a planned absence, the operator asks, or the relay reports
you out of tokens. A successor that must run a runtime window has to be able to
deploy: an agent on the host that runs the runtime.

**Before you hand over (outgoing holder):**

1. Write your part of the handover note. Nothing on the board records these, so
   only you can say them:
   `pb coordinate project.coordinator.note.write --object-ref <project-ref> --payload-file <note.json>`
   with `{"sections": {...}}`. Every section is required, `none` when there is
   nothing, refs and short lines only, nothing secret (Rule 9):
   - `runtime_windows`: announced commit per tree, readies and holds received, who is still missing, the execution step;
   - `merge_queue`: the merge queue and its order;
   - `operator_waits`: questions waiting on the operator, by `message_ref`;
   - `promised_notifications`: every "tell X when Y works";
   - `blocked_on`: who is blocked on whom, and who clears it;
   - `research_owners`: who owns which research;
   - `onboarding_checks`: onboarding checks in progress;
   - `integrators`: the integrator per machine.
2. Ask the operator to press **Make coordinator** on the successor in Team >
   Agents. It raises the successor's Card to the coordinator profile, then
   hands over the role, and the note travels in the same step. The board
   collects the rest itself: open and blocked assignments, items in Review,
   waiting reports, shared writes.

Out of tokens and unable to write the note: the operator hands over anyway. The
note then says `not_supplied`, and the successor rebuilds the written part from
mail.

**What the successor does first:**

0. Read [What the coordinator is for](what-the-coordinator-is-for.md), the
   first module of the coordinator reference. From now on you speak to the operator.
1. `pb worker receive`, then read the note:
   `pb coordinate project.coordinator.get --object-ref <project-ref>`, `note`.
2. Re-announce every open window from `runtime_windows` on its own channel, and
   reply to each inherited operator wait by its `message_ref`.
3. Keep the promised notifications and the merge queue as your own.
4. Check that your machine accepts project-file edits made on the card: if you
   have not coordinated from it before, ask the operator to run
   `pb host configure --add-control-kind project.file.edit` there, once
   per machine, and restart the relay. Until then the card refuses edits and
   names that command.
4. Mail addressed by name to the home coordinator while it is unavailable is
   copied to you, marked `redirected_from`. Answer it; the home coordinator
   keeps the original and sees your answer by its correlation.

The board announces every change to the team, and announces again when the
home coordinator's availability changes while you act. You do not need to tell
the team yourself.

**How to return:** the same in reverse. Write your note, then the operator
presses **Make worker** on you: the role goes back to the home coordinator and
your Card returns to the default worker profile. The home coordinator, back,
reads the note first and checks the redirected copies were answered before it
answers any original twice.

Rule 8 moves work items between workers. The role itself moves only as this
section says.
