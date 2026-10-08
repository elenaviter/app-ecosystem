---
id: project-board.skill-reference.source-and-review
title: Share The Repository With The Other Workers
summary: How every worker changes shared source - its own registered tree, a branch and change request, visible intent, the checks before a review, the reviewer's exact-head tree, milestones and landing an approved change.
tags: [procedure, problem-board, worker, source, review, merge]
keywords: [working tree, git worktree, pb worker workspace, branch, change request, shared-write dashboard, source_in_flight, rebase, force-with-lease, merge-base, review tree, exact head, approval, merge commit, completed report, landing, documentation]
see_also:
  - ./collaboration.md
  - ./project-workspace.md
---

# Share The Repository With The Other Workers

Read this in full before your first edit in a shared repository, before you ask
for a review, and before you review or merge one.

Several agents work on the same repositories at once. The rules that keep
them apart are the collaboration procedure, [collaboration](collaboration.md),
revised one rehearsal round at a time. What every worker does, from it:

- **One working tree per agent, registered.** Register every tree you create (`pb worker workspace --path ... [--kind review]`); a sweep removes finished, clean, fully pushed trees at session start, on idle and after a review decision ([project workspace](project-workspace.md), section 6). Develop in your own clone or `git worktree`.
  Never edit a shared checkout except to land an approved change (below), and
  leave nothing of yours there. Why: a branch does not separate files on disk,
  and on a machine where the shared checkout is also the live `pb` runtime an
  edit there is live for every worker at once.
- **Work on a branch, exchange through a change request.** Branch
  `work/<wN>-<short-slug>` from the pushed integration ref (`origin/main`), push
  it yourself (the operator's ruling of 2026-09-22), and open a change request
  against `main` when the work is ready for review. Commit each coherent piece
  as you finish it. Put the link on the item and in your report. The merger the item's route names (a permitted non-author), or the coordinator when none is named, merges after approval and pushes the integration ref, with no further acknowledgement.
  Deploying stays the operator's. A branch is closed by its merge, a later push is a
  new change request, and you delete your branch when it merges or you abandon it.
- **Publish your intent before the first edit** on the shared-write dashboard
  (`kind=source_in_flight`, the item key, the repository paths you will
  touch), read the list first, and send an overlap to the coordinator rather
  than settling it with the other agent. The dashboard grants nothing and
  blocks nothing. Clear your dashboard entry when the change request is open.
  TTL is recovery for an abandoned entry, not the completion path. Operations:
  `workspace.shared_write.list`, `workspace.shared_write.publish`,
  `workspace.shared_write.clear`, invoked and shaped as in [collaboration Rule 3](collaboration/rule-3-make-your-intent-visible-before-you-edit.md).
- **Before you ask for a review:** `git merge-base --is-ancestor origin/main
  <head>` (every integration push moves the base under every open change
  request), rebase with `--force-with-lease` on your own branch when it fails,
  run the suites on the head you name and state the counts, show that a
  regression written for a finding fails without the fix, and list every place
  the rule you changed is enforced. A claim about what a host installs is
  settled by installing it into a fresh environment at the named commit, not
  by reading a `pyproject`. Nothing non-public in a public repository's
  branch, commits, description or comments. Approval is a board mail naming the
  head, quoted on the change request: GitHub sees one account for all agents
  and refuses its own author.
- **As reviewer or merger, use the detached exact-head review tree under
  `<workspace>/rv/` that [project workspace](project-workspace.md),
  section 6 defines, then compare your count with the author's** and ask about
  the difference: a skip names its missing input, and a suite that skips what
  the change touches is green about everything except the change. Your approval
  states the exact head, the files the change request lists and the files you
  read, and suite inputs (interpreter, dependencies, overlays, variables), so
  the counts can be compared at all.
- **A `completed` report submits the source for review** at an exact head and
  change request, and its could-not-verify names what is still to come (merge,
  activation). Approval, merge, activation and whole-item acceptance are
  separate milestones ([collaboration Rule 6](collaboration/rule-6-your-visible-state-says-where-you-are-and-what-you-ar.md)). The merge milestone names the merge commit after you fetched and ran `git merge-base --is-ancestor <commit> origin/main`. The acceptor runs it on their own clone. Any
  claim that a change landed (a report, an item note, a journal lesson) names that merge commit after you fetched it, never the intention
  to merge. With its documentation: when behaviour a doc describes
  changes, the doc changes in the same item, because undocumented behaviour is
  how a diagnosis goes wrong. One home per concept, one-line pointers
  elsewhere, no links to gitignored paths.
- **Landing an approved change into a shared checkout** (no coordinator, no
  elected integrator) follows the Interim steps in
  [collaboration](collaboration.md). Never `git add -A`, never `git stash`.
