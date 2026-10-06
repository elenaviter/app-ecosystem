Part of [collaboration](../collaboration.md).

## Rule 7. A question about how the team collaborates is decided in rounds

When the team has to choose how it collaborates or stays visible (a practice,
a channel, a mechanism that changes what each member publishes), one member
proposes and runs the decision. The rounds come in this order, and none is
skipped:

1. **Independent ideas.** The proposer sends every member the questions, and
   only the questions: no candidate practices, no coordinator input, nothing
   from anyone else's answer. Each member answers at its next safe boundary,
   in its own words. The proposer writes its own answer before any other
   arrives. Nothing is shared until every answer is in, or a member is shown
   as `pending`.
2. **Read all.** The proposer sends every member every idea, attributed, in
   the words it came in.
3. **Talk again.** Members react, build on each other's ideas, and amend or
   change their position. The proposer runs another exchange while the
   discussion still moves.
4. **Votes and result.** From the positions after the talk, the proposer
   writes the result. At the top, a votes table: one row per candidate
   practice, one column per member, each cell that member's vote with a
   one-line reason in its words, `pending` for a member who has not answered
   and never a guess, and a decision column reading `adopted`, `dropped` or
   `open`. Below the table, every member's thoughts, attributed. Split votes
   stay `open` for the operator. The result goes to the operator and to every
   member, not only to the coordinator.

Each round has a time box the proposer states with the questions. A member
who has not answered when it closes is shown as `pending`, and the owner of
the current P0 work may skip a round. The proposer reads each member's
availability ([Rule 16](rule-16-every-task-has-a-living-route-and-each-actor-knows-i.md)) when it sends the questions and
again when the time box closes: an unavailable member is shown as `pending`
with its reason, never counted as agreeing, and a pending poll never holds
work that is already approved and does not depend on its result. A change to a procedure is decided this
way too, and the rules it adopts land in the procedure section that owns them
through a reviewed source change, with a behavioural check, not only in a
note. An operator ruling on the question is adopted as given and is not put
to a vote (W449).

Where the result lives: as a note on the item the question belongs to
(`plan.note.append`), so a member on another machine or a successor after a
handoff reads it from the board with no checkout at hand. The mail to the
members and the operator carries the same text and names the item. A journal
entry may narrate how the decision was made. The note is the record.

Why: a poll that shows candidates first gets the candidates back. Members who
read each other only after they have thought bring ideas the proposer did not
have, and a result everyone saw being made is one everyone follows. The
operator's words, 2026-09-23: "make the polls and think together, and then
show me and everyone the thoughts of everyone", and "first everyone makes the
idea and then they can read all ideas and then talk again".
