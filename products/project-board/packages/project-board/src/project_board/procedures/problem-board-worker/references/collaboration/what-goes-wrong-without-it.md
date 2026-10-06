Part of [collaboration](../collaboration.md).

## What goes wrong without it

All of these happened on 2026-09-22, on one machine, in one evening:

- A finished change could not be committed because another agent's half-done
  files sat in the same working tree (one agent's W253, blocked by another's
  W272).
- An agent reported another agent's uncommitted file as the coordinator's, and
  a reload would have staged that unreviewed edit (the same hour).
- A change that passed every test on the host would have broken the container
  at the next reload, because the runtime imports from a path the change had
  moved (the `project_board.contract` carve).
- One rule lived in three places and each fix found the next: the server, the
  widget's draft reducer, then `review.return` (W245).
- Two agents could not settle an overlap between themselves, because a Codex
  worker reads mail between turns: "agree it with the other agent" had no
  bound either could see.

Each rule below names which of these it removes.
