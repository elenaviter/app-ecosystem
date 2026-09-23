---
id: project-board.worker-reference.coordinator
title: Accept, Route, Reload, Refresh
summary: The coordinator's checklist for the acts a coordinator performs (review decisions, release, routing, runtime actions), placed where the act happens rather than in onboarding prose, because a rule read at onboarding was followed for a day and then skipped at the moment it mattered.
tags: [procedure, problem-board, coordinator, review, routing, runtime]
keywords: [review.accept, assignment.return, release assignment, idempotency_key, work_review_self_forbidden, route, discuss before routing, shared-write dashboard, ready or hold, hold release, missing answer, preflight, bundles.template.yaml, bundle reload, refresh --build, widget build states, verify the artifact, what loaded]
see_also:
  - runtime-actions.md
  - test-window.md
---

# Accept, Route, Reload, Refresh

Read this when you are about to accept, return or cancel a submission, release
a stalled assignment, route an item, or reload, refresh or restart the runtime. Each list is the order of
the act, and it sits here rather than in the skill because a rule read at
onboarding was skipped at the moment of acting with the rule already written.

## Accept, return, cancel

1. The submission is read against the item's acceptance lines, one by one, and
   against the deployed artifact where a line is about behaviour (see Reload
   below for what "deployed" means per tree).
2. A submission that closes a line with "that case cannot occur" is returned,
   not accepted, until it names the observation that would show the case
   occurring. A claim is derived from the observation that would falsify it
   and carries that observation's timestamp. A name, a timer or a threshold is
   not one.
3. `review.accept`, `review.return` and `review.cancel` take the item
   `work_ref` looked up from `project.plan.item`, its `expected_revision`, and
   an `idempotency_key` you generate for this decision. Return and cancel take
   a reason. The service refuses the worker that submitted the work from
   deciding on it (`work_review_self_forbidden`), for all three decisions.
4. The item is the record. After the decision, read the item back: status
   `done` for accept, `todo` with the same assignee for return (the worker
   keeps the assignment, its ownership version advances, and it reworks
   against the new version), `cancelled` for cancel, and the assignment state
   beside it. To hand returned work to someone else, release it with
   `assignment.return` and its reason, then assign. Mail about the decision is
   commentary.

## Release a stalled assignment

1. `assignment.return` (Release assignment) takes the item `work_ref` from
   `project.plan.item`, its `expected_revision`, a reason, and an
   `idempotency_key` you generate. It needs an active assignment.
2. Release changes ownership only: the assignee is cleared, the released
   worker becomes the preferred reworker, and the ownership version advances,
   which refuses any later report from that worker. Status stays as it was.
   Why: assignment and status are separate facts, so neither assigning nor
   releasing says anything about progress.
3. To also change the status, make that a separate status edit, by a caller
   permitted to set status.
4. Tell the released worker, with the reason, in a correlated message. Then
   route the item again or leave it for the plan.

## Route

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

### Bind every repository the work touches when you assign

An assignment carries `source_repositories`: one entry per repository the
work touches, each with its repository ref, the base commit the worker starts
from and the branch it works on. Fill it when you assign, not later: W212
spanned two repositories and W255 three, and every one of tonight's
assignments went out with the binding blank, so a reader of the board saw a
worker with open files and could not say which item they were for. Work that
touches no repository says so with an empty list; an assignment with no list
at all reads as `repositories not declared`, which is a defect of the
assignment, not a property of the work.

```json
{"source_repositories": [
  {"repository_ref": "repo:kdcube-ai-app/app", "base_commit": "947238921", "branch": "work/w212-picker"},
  {"repository_ref": "repo:app-ecosystem/products", "base_commit": "83f6d21ab", "branch": "work/w212-cards"}
]}
```

## Put what a worker must read where that worker can read it

A route that points at something the assigned worker cannot open is not a
route. Before sending a pointer, ask what that worker's Card can read and what
its machine can reach.

- **Text the worker needs in order to do the work belongs in the item**, in its
  description or in the assignment's task instructions. Those travel with the
  assignment, survive an unread inbox, and every worker Card can read them
  through `project.plan.item`.
- **Notes are the record, not the briefing.** Worker Cards do not grant
  `plan.notes.list` today, so "read the newest note" refuses. Decide by what the
  Card grants, not by what the coordinator can see.
- **Never send a local filesystem path as the carrier.** A path is bound to one
  machine and one user. The moment a worker runs on another host it points at
  nothing, and the failure looks like a worker ignoring instructions.
- **Attachments are operator-only.** Worker-to-worker mail refuses them with
  `field_attachments_operator_only`; local worker mail carries paths in its
  body, which is subject to the rule above.
- **A repository ref is portable, a working-tree path is not.** Point at a
  committed file by repository alias and path, never at `/home/...` or
  `<home>/...`.

A refusal a worker reports while following a route is the coordinator's defect
first: fix how the work was handed over, and raise the grant when the refusal
was the right rule applied to the wrong case.

## Reload, refresh, restart

The action stages the working tree, whatever it holds at that instant. The
list is what makes that survivable until an activation takes a commit.

1. **Read the dashboard first**, and act on each row. The row says what a
   worker is about to change and `git status` says what has changed. A
   `source_in_flight` row with targets under the tree you are about to
   stage, and a tree that is dirty anywhere, holds the action until that
   worker commits or clears, whether or not git shows the named path yet:
   the write git cannot see yet is the one you can still avoid staging. The
   same row with a clean tree is a worker that has declared and not begun,
   and a reload then stages committed state only: ask its owner, now or
   after, and act on the answer, because the holder decides its own hold.
   Name the row's owner in the announcement either way. A `reload` row is
   a request and never a hold: a `source_in_flight` row says do not stage
   me and a `reload` row says please stage me, and a coordinator that
   treats every row as a hold is blocked by the request asking it to
   proceed.
2. **Announce** the action, the tree, and what it releases (a worker may
   have published the activation it asks for as a `reload` row with
   targets `bundle:<id>` and `procedure:<package>@<revision>`, so the
   others see a reload coming, and which revision, before the mail): the commits since
   the last activation of that tree (`git log <last>..HEAD -- <tree>`), one
   line each, naming any that are unreviewed. Collect one `ready` or `hold`
   from every attending worker. One `hold` stops it. A `ready` that carries a
   constraint (a commit it must be at or after, a window it needs, a file it
   is about to touch) is honoured or the action is re-announced. A `hold`
   names what releases it (a commit, a clear, a time) and the holder sends
   the release. A hold without a condition is asked for one, and when its
   condition is met and verified (the named commit on `HEAD`, the row's paths
   clean) the action may run with the hold quoted. A worker that has not
   answered is asked once more with the deadline. Running without its answer
   is allowed only when its dashboard row and `git status` both show nothing
   of that worker's under the tree being staged, and the announcement records
   the missing answer and that reason.
3. **Immediately before**: `git status --porcelain` on the tree and a
   `pb worker receive`. Both are evidence about that moment and neither is the
   guarantee: the tree can change between the check and the staging, and the
   guarantee is an activation addressed to a commit. When the range touches
   `bundles.template.yaml`, diff the touched entry against the live
   `config/bundles.yaml` first: a fix whose descriptor is behind it deploys
   and cannot run. Identical blocks sit under different bundle ids in that
   file, so edit the live descriptor by locating the bundle id, never by the
   first match of a block.
4. **Execute** the action `runtime-actions.md` names for the tree. A bundle
   reload returns before the widget build finishes, and a widget has three
   states after a reload: build pending, no build because the signature was
   unchanged and the artifact is already current, and no build because it
   broke. The receipt does not tell them apart, only step 5 does.
5. **Verify the deployed artifact, never the commit.** Bundle: the eviction
   count plus one symbol or behaviour the change introduced, asked of the
   running process. Widget: `dist/` inside the container carries the new
   source, after the build ends. Relay: the first stamped line of the new pid
   (`file_descriptor_limit=`, `source=`). Package: `pb procedure verify` on
   the host. Descriptor: `bundle status` on the running catalog.
6. **Say what loaded**: the pid or eviction count, the commit range, and, for
   each worker whose commits rode along, that they did. Clear your dashboard
   row. A worker asking "what did that release" is asking for this line.

A LaunchAgent or systemd unit is host service configuration: `pb relay-service
install` is typed by an agent after the operator approves it, and
`kdcube refresh --build` rebuilds her stack, so both are hers to clear.
Restarting a service whose definition exists is a coordinated runtime action
and needs no more than the list above.

`pb source use-release` and `pb source use-code` change the immutable source
used by both the host command and that service, and include the restart needed
for the relay to observe it. Announce the exact package version or, for code,
the full App Ecosystem and KDCube commits. Collect the same host-local
readiness, and verify `pb source status` plus the new relay startup record
before reporting the move complete.

When a host still runs the checkout client, follow the cutover section in
[runtime actions](runtime-actions.md) before fast-forwarding that checkout.
The six-package source install, two-commit verification, and relay restart are one
precondition for the fast-forward, not recovery steps after it.

## Research Is Coordinated Progressively

The coordinator names one researcher for a question and tells the other workers
who owns it; others continue their assigned work. The researcher returns concise
findings with a `repo:<alias>/<path>` link per source plus line or symbol
detail. Receivers assess them before building on them and ask the researcher for
targeted verification of an uncertain fact. A second investigation starts only
when the coordinator or operator names a specific reason. (Moved here from the
skill in revision 2026.09.23.3 to make room for the brief-output rule: content
moves, prose is not compressed.)
