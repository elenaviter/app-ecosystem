Part of [coordinator](../coordinator.md).

## Route

0. Read the candidates' info lines before routing: the `info_text` of each
   member in `pb worker context`, the first line on each card. Why: an agent
   publishes there what the operator told it about itself (not to be used
   actively, reviews only), and routing past it spends a quota or a session
   the operator reserved.

   **Carry an operator restriction to the card before routing from it.**
   When the operator sets or changes a restriction on a worker (a cap, a
   pause, reviews only, a resumption):
   - Record the ruling in the project Facts: what holds, who ruled, since
     when.
   - Ask that worker, when it is active, to publish the exact restriction
     with `pb worker info write` ([collaboration Rule 6](../collaboration/rule-6-your-visible-state-says-where-you-are-and-what-you-ar.md),
     The info line). A paused worker is not woken for this. Until it
     returns, the Facts row is what routing reads.
   - Before you say the restriction is in place, or route from it, check
     that the worker reports its `pb worker info show` with `on_board = True`
     and that its row in a fresh `pb worker context` team shows the same
     `info_text`. You cannot run its `show` yourself; you rely on that
     reported receipt and your own fresh team read.
   - The restriction stays first on the line and every status rewrite keeps
     it. Findings and progress detail go to mail, item notes, the task and
     `pb worker busy-until`, never in place of the restriction.

   Why: on 2026-09-30 the operator let one paused agent resume under a
   weekly ceiling on its shared account's total usage. The ruling reached the
   project Facts and the team's mail, and the agent's card showed no
   restriction until the operator asked. Step 0 already said to read the
   line, so this was an execution failure. Nothing required the restriction
   to reach the card before the coordinator routed from it (W413).
1. Need, then discussion with the candidates, then decision, then route. A
   route carries the intention and the acceptance, not the engineering
   constraints. The assigned worker decides how.
2. Every ref in the route is looked up and copied whole: `project.plan.item`
   prints `identity_ref`. A ref composed from a key and a title is refused or,
   worse, accepted and wrong.
3. Name the item by key and title in every mention. A bare number is a lookup
   the reader has to make.
4. Search the plan before filing. A finding that already has an item gets a
   note on that item.
5. Set the new item's dependencies in the same call. `depends_on` lists the
   `identity_ref` of every item that must land first; an item that is related
   but does not block goes in the description as "Related:". Recheck both
   directions when you rescope an item or split out a step. Why: on
   2026-09-25 six items (W318 to W323) went out with none, and the order
   (W322 needs W323, W318 needs W313) lived only in one coordinator's head,
   which a context reset or a hand-over loses.

### Record each dispatch on the item

When you dispatch work or change its phase (an assignment, a review routing,
a poll, a handoff between agents), append one note to the item
(`plan.note.append`). It names:

- the recipient's full alias and stable worker name;
- the phase and its scope;
- the assignment or control reference and its ownership version;
- why this owner and this phase now, in one line;
- the evidence state, one of requested, queued, applied or STARTED, never
  merged into one;
- the source and job constraints;
- the next checkpoint, or that it is unknown;
- the expected deliverable and the next owner and action.

The item's description carries the task's living route
([collaboration Rule 16](../collaboration/rule-16-every-task-has-a-living-route-and-each-actor-knows-i.md)): write it when you first route
the item, and keep it current at every change of actor or phase, saying in
the description which earlier instructions it supersedes. A route with an
open technical question names who answers it and by when; a missing detail
never leaves the route without an owner or waiting on your acknowledgement.

Link journal and source evidence instead of copying it into mail. The
assignment stays authoritative; the note explains the handoff. After a long
gap, read the item, its assignment and the latest handoff note before old
mail, and say which earlier instructions are superseded (W449).

### Route through Problem Board's worker CLI, with the complete assignment payload

The coordinator uses the same installed worker interface as every other
agent. `pb worker context --project-ref <project-ref>` supplies the stable
worker name, project workspace and repository evidence. `pb coordinate
project.plan.item --object-ref <project-ref> --payload-json
'{"item_key":"<Wn>"}'` supplies the current item, its complete canonical refs
and revision. The published operation procedure supplies the payload below;
`pb coordinate --help` supplies the common transport arguments. Route it with:

```bash
pb coordinate assignment.assign \
  --object-ref <project-ref> \
  --payload-file <assignment.json>
```

```json
{
  "work_ref": "<identity_ref copied whole from project.plan.item>",
  "worker_name": "<stable worker_name from pb worker context>",
  "title": "<assignment title>",
  "task": {"instructions": "<bounded briefing; the item carries acceptance>"},
  "expected_ownership_version": 0,
  "source_repositories": [
    {
      "repository_ref": "repo:<registered-alias>/<exact-relative-scope>",
      "base_commit": "<full commit>",
      "branch": "work/w<N>-<slug>"
    }
  ],
  "source_repository_ref": "<copy source_repositories[0].repository_ref>",
  "source_base_commit": "<copy source_repositories[0].base_commit>",
  "source_branch": "<copy source_repositories[0].branch>",
  "idempotency_key": "<stable key for this routing decision>"
}
```

`expected_ownership_version` is `0` only for a never-assigned item; for a move
or reissue copy the current assignment row's value. Work touching no repository
uses an explicit empty `source_repositories` and omits the one-repository mirror.
Every `repo:` value is copied from current project or item evidence. If no
authoritative read exposes it, fix that record or the CLI projection first;
never guess it from a clone name. When the operation is refused, preserve the
returned code and fields, consult the operation procedure and retry only the
documented recovery; an outcome-unknown result repeats the identical request
under the same idempotency key.

### Bind every repository the work touches when you assign

An assignment carries `source_repositories`: one entry per repository the
work touches, each with its repository ref, the base commit the worker starts
from and the branch it works on. Fill it when you assign, not later: W212
spanned two repositories and W255 three, and on 2026-09-23 every assignment
went out with the binding blank, so a reader of the board saw a worker with
open files and could not say which item they were for. Work that touches no
repository says so with an empty list. An assignment with no list reads as
`repositories not declared`, and the coordinator corrects it by reassigning
with the list.

```json
{"source_repositories": [
  {"repository_ref": "repo:kdcube-ai-app/app", "base_commit": "947238921", "branch": "work/w212-picker"},
  {"repository_ref": "repo:app-ecosystem/products", "base_commit": "83f6d21ab", "branch": "work/w212-cards"}
]}
```
