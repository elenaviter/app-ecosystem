---
id: project-board.worker-reference.collaboration
title: Collaborate Across Workers
summary: How workers isolate source changes, exchange them through change requests, resolve overlap through the coordinator, and prove the exact revision that is ready to merge.
tags: [procedure, problem-board, collaboration, git, change-request, coordinator]
keywords: [worktree, branch, pull request, merge gate, source in flight, current base, runtime import]
see_also:
  - ../SKILL.md
  - coordinator.md
  - runtime-actions.md
---

# Collaborate Across Workers

Several workers may change the same repositories from different machines.
This procedure keeps their files, Git indexes, runtime source, and decisions
separate until a reviewed change request joins them.

## One Working Tree Per Worker And Item

Develop in a private clone or `git worktree`. Never edit a checkout another
worker uses, including a checkout used by a live runtime. A branch separates
history; a working tree separates files and the Git index.

Create each item branch from the current pushed integration ref:

```bash
git fetch origin main
git worktree add <workspace>/<repository>@w<N> -b work/w<N>-<slug> origin/main
```

Use a sibling worktree when a second item starts before the first change
request merges. Remove your own item worktree and branch after merge or after
you explicitly abandon it. Do not remove another worker's worktree or branch.

Problem Board commands and the relay use the target's selected immutable
source. A development checkout may be invoked directly for a test, but that
process is visibly unpinned and does not change the host selector.

## Exchange Work Through A Change Request

Push your own `work/w<N>-<slug>` branch and open a change request against the
integration ref when the branch is ready for review. Put its link on the work
item and in the review request. The change request, not a machine-local path,
is the review surface.

The coordinator merges after approval. The author does not merge their own
change request. A change spanning repositories links every change request to
the same work item and states the required merge and release order.

Public repositories require public-safe branch names, commits, descriptions,
comments, docs, fixtures, and tests. They contain no credentials, customer
names, private hostnames, private filesystem paths, or links readers cannot
open.

## Publish Source Intent Before Editing

Read the shared-write dashboard and publish one `source_in_flight` entry before
the first edit. Name the item and every repository path or ownership boundary
that may overlap another change.

```bash
pb coordinate workspace.shared_write.list \
  --object-ref <project-ref> \
  --payload-json '{}'

pb coordinate workspace.shared_write.publish \
  --object-ref <project-ref> \
  --payload-json '{"kind":"source_in_flight","summary":"W123: move one client contract to its package owner","targets":["repo:product/packages/client","repo:runtime/requirements"],"ttl_seconds":14400}'
```

An overlapping entry is a coordination signal, not a lock. Send the overlap
to the coordinator and continue only on non-overlapping work. The coordinator
decides ownership and merge order. Clear your entry when the change request is
open; expiry is recovery for an abandoned entry, not normal completion.

```bash
pb coordinate workspace.shared_write.clear \
  --object-ref <project-ref> \
  --payload-json '{}'
```

## The Coordinator Resolves Shared Decisions

A decision that joins two workers' changes goes to the coordinator with both
sides named. The coordinator records the ruling on the item so every worker
reads the same durable decision.

This includes:

- ownership of a shared file or package boundary;
- merge and release order across repositories;
- whether one change waits for another;
- resolution of a semantic merge conflict;
- timing of a reload, refresh, source selection, or relay restart.

When blocked behind another change, report what overlaps and take another
non-overlapping assignment. Do not wait silently or edit through the overlap.

## Merge Gate

Before asking for review, and again after every integration push, prove the
following against the exact head named in the request:

1. **Current base.** `git merge-base --is-ancestor origin/main <head>` exits
   zero. If it does not, rebase your own branch, rerun affected suites, and
   push with `--force-with-lease`.
2. **Green suites.** Run the suites for every changed ownership boundary on
   that head and state their counts. A regression written for a finding also
   demonstrates that the unfixed behavior fails.
3. **Real constraints.** A fake models each database uniqueness, identity,
   ordering, or fencing rule used by the proof; otherwise run the regression
   against the real service.
4. **Runtime path.** When imports, packages, or deployment inputs move, state
   how the runtime receives them, in which order, and which command proves the
   running artifact loaded them.
5. **Complete enforcement.** List every server, client, operation, projection,
   and document that enforces the changed rule, and state how each was checked.
6. **Documentation.** Behavior and its owning documentation move together.
   Other docs point to that owner instead of restating it.
7. **Public content.** Recheck the branch, commits, change-request text, and
   comments for information that cannot be published.
8. **Independent approval.** The named reviewer is not the author and names
   the exact approved head. When the hosting account is shared, the durable
   board message is the review record and is linked from the change request.

The coordinator merges only when every gate holds. A new push creates a new
head and invalidates approval and test claims tied to the previous one.

## Keep Visible State Current

The board must show which item the worker owns, its branch and change request,
what source area is in flight, and what decision or review it awaits.

- Report `working` when work begins and name the branch once it exists.
- Add the change-request link when it opens.
- Record a wait and its correlation instead of going silent.
- Report `completed` only after the change request is merged, unless the item
  explicitly defines another terminal condition.
- Cite the event that prompted each report. A source event is spent once.
- When review returns the item, keep its branch and change request, use the new
  ownership version, and cite the returned-work notice in the next report.

The integration ref is source history, not a runtime selection. Merging or
fast-forwarding it never advances a host's Problem Board source. A coordinated
`pb source` action selects the approved package version or full commit, and the
relay observes that selection only after its restart.
