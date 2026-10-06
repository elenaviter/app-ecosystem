Part of [coordinator](../coordinator.md).

## Refresh the evidence you decide from

Freshness belongs to the decision boundary, not to the session. Immediately
before routing, review, hand-over, merge ordering, or a client/runtime choice,
rerun the smallest read that supplies that decision's facts. A result from an
earlier boundary, a compacted conversation, or private memory is not current
evidence.

Availability is such a fact. Read it (step 1 of "Put the team to work" defines
it) before you assign or route, before you form the list a poll or a window
waits on, and again when you interpret the answers or a silence, when a
handoff is consumed, when you choose an item's next action, and when you
learn that someone's availability changed. Silence is never consent,
approval or READY. A verdict or reply completed before its author became
unavailable stays valid evidence for its exact head, and proves no current
capacity.

- Refresh the project, holder, team, quotas, repositories and workspace with
  `pb worker context --project-ref <project-ref> --format brief`.
- Search only the named subject in the journal with
  `pb worker journal-search --project-ref <project-ref> --query <subject>
  --limit <small-number> --format brief`.
- Use `project.plan.search` with the subject and a small `limit`, then
  `project.plan.item` for the exact returned key or ref. Do not page or assemble
  the plan to make a decision about one subject.
- Use `assignment.list` with the worker plus the narrow refs, status or query
  that the ownership decision needs; keep its `limit` small.
- Run `pb source status --format brief` immediately before deciding which
  client or relay source is actually selected and running.

These brief reads keep every displayed ref, cursor and commit copyable whole.
If a decision needs a field or prose omitted by the summary, rerun that same
narrow command with `--format json` and read the full envelope directly, unless
it carries attachments ([brief-output](../brief-output.md)). Do
not replace a fresh targeted read with local `jq`, a hand-written parser, or a
large cached snapshot.
