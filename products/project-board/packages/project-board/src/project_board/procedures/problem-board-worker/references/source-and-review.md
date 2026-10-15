---
id: project-board.skill-reference.source-and-review
title: Share The Repository With The Other Workers
summary: How every worker changes shared source - its own registered tree, a branch and change request, visible intent, the checks before a review, the reviewer's exact-head tree, milestones, landing an approved change, and confirming it is in main before Done.
tags: [procedure, problem-board, worker, source, review, merge]
keywords: [done, main ancestry, merge-base --is-ancestor, working tree, git worktree, pb worker workspace, branch, change request, shared-write dashboard, source_in_flight, rebase, force-with-lease, merge-base, review tree, exact head, approval, merge commit, completed report, landing, documentation]
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
- **A contract its consumer checks at import is reviewed and merged with that
  consumer.** When a change adds, removes or renames an operation in App
  Ecosystem's operation contract or catalog (`worker_operation_contract.py`,
  or any contract the Applications board checks when it loads), the review
  evidence includes the Applications suite run against that exact App
  Ecosystem head, and the change merges only together with, or after, the
  Applications change that handles it, never App Ecosystem alone. Why: on
  2026-10-13 W815 added `project.delete` to the contract and was approved on
  the App Ecosystem suite alone (2274 passed); Applications'
  `operation_dispatch.py` checks at import that every declared operation has
  a handler, so its fixtures failed once App Ecosystem main moved ahead, and
  the merge was reverted (acdbb4f2) until W805's handler lands (W820).
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

## Before Done, the repository work is in main (W685)

Whoever moves an item to Done (an agent, its own author included, or a
person) first confirms that the item's repository work is in `main`. It is a
soft rule, kept by that actor and not refused by the service; Connection Hub
plays no part. The operator, relayed by EMain on 2026-10-10:

> "its a team who decide. when you work on something you merge the stuff from time to time to some intergrational branches. when the integrational branches are proven and merged to main (i really do not care - either intergrational branch or the smaller branches - imporatnt is that the stuff that was done must be first acknowledged in main by hwomever who moves the task to done)"
> (`work:mail:20261010T144955643960Z:mail_7c0d8bd2160c4d4eba90579cbf66ea00:w685-operator-fixed-acknowledged-in-main-by-whoever-moves-to`)

> "not only person. the agent itself also must be able to move the task to done. this is soft rule."
> (`work:mail:20261010T145118474187Z:mail_82c362a6a67d4260a2b5cbc5b456770c:w685-operator-verbatim-the-agent-itself-may-move-its-task-to`)

> "however this is not related to all tasks" / "some tasks even do not have no any github stuff related"
> (`work:mail:20261010T144931465275Z:mail_9bd620777c184eb0aa3962a0fd007b1a:w685-scope-operator-verbatim-only-tasks-with-repository-work`)

- **The check.** Fetch, then for each repository the item changed run
  `git merge-base --is-ancestor <merge commit> origin/main`. Integration or
  smaller branches may carry the work there; a tag, an author's word or an
  unmerged integration branch is not the confirmation. For a squash or a
  rebase, name the integration commit and how it maps to the reviewed head;
  a missing author-head ancestor is not, on its own, a failure.
- **Live is a separate claim.** When the item says the work is live, the merge
  commit is also an ancestor of the head the runtime runs (the deploy tree's
  head, matched by Source Attestation). In `main` never means installed or
  live ([delivery states](coordinator/accept-return-cancel.md)).
- **One receipt, on the item.** A dated note, linked from Result, gives per
  repository the delivered head, the integration commit or change request,
  the `main` SHA checked and when, who checked, and the outcome. It covers
  every repository and phase the item needs: one merged phase is not the
  whole item.
- **Say whether there is repository work.** The item states "repository
  work: yes (<repositories>)" or "none". Work with none (a decision, setup, an
  operator task, a live check) names what it delivered by exact ref, with no
  made-up commit and no Git exemption.
- **The author may close its own item** after the review the item needs and
  the merge. Its `main` check is a check, never a review and never
  `review.accept`.
- **A Done item later found not in `main`** is corrected by a dated note and,
  when the absence is proven, a reopen; the original Done, results and reviews
  stay.

Why: W826 was marked Done while its pull request (#812) had never been
merged; it was found, rebased as #849 and merged as 7b4f1506 only on the
night of 14 to 15 October (EMain, 2026-10-15). The practices above are the ones the team adopted in W685's Rule 7 round
(`work:note:20261010T163028853418Z:note_9b2e14f55e7d9010d98152487ba1744b:w685-rule-7-consultation-round-4-votes-and-result-proposer`).
Its open questions (whether a merger's receipt may be cited, how
`review.accept` relates to Done, outcomes for a historical sweep) are the
operator's and are not decided here.
