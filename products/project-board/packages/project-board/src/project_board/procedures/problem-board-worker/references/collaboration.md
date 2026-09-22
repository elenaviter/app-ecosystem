---
id: applications.playground.problem-board.skill-reference.collaboration
title: Collaborate Across Agents And Machines
summary: How several agents on several machines work on the same repositories without blocking or overwriting each other, exchange work through change requests, and integrate through the coordinator. Written to be run, observed and revised, one rehearsal round at a time.
tags: [procedure, problem-board, collaboration, git, change-request, coordinator, multi-machine]
keywords: [one tree per agent, work branch, change request, pull request, merge gate, integration ref, intent before edit, shared write, coordinator decides, rehearsal round, collision log]
see_also:
  - ../../agent-worker.md
  - ../SKILL.md
  - ./operator.md
  - ./add-a-worker-host.md
  - ./problem-board-worker/SKILL.md
  - ./problem-board-worker/references/coordinator.md
  - ./problem-board-worker/references/runtime-actions.md
---

# Collaborate Across Agents And Machines

**Status:** first pass, 2026-09-22, owned by fable-pub (W262). Usable, not
complete. It is a loop, not a document: the team works under it, what
collides is recorded in the last section, and the rules change on what was
observed. The measure is the count of collisions and mistakes going down from
one round to the next.

**Written for:** several agents on several machines. Today that is dev-main
(four agents) and spark1 (two agents, `procedures/add-a-worker-host.md`).
Nothing below assumes one checkout, one host, or that two agents can talk to
each other in real time. One machine is a fair test of every rule except the
cross-host paths, so the loop starts on dev-main before spark1 is authorized.

## What goes wrong without it

All of these happened on 2026-09-22, on one machine, in one evening:

- A finished change could not be committed because another agent's half-done
  files sat in the same working tree (codex-ui's W253, blocked by codex-main's
  W272).
- An agent reported another agent's uncommitted file as the coordinator's, and
  a reload would have staged that unreviewed edit (fable-pub, the same hour).
- A change that passed every test on the host would have broken the container
  at the next reload, because the runtime imports from a path the change had
  moved (codex-ui's `project_board.contract` carve).
- One rule lived in three places and each fix found the next: the server, the
  widget's draft reducer, then `review.return` (W245).
- Two agents could not settle an overlap between themselves, because a Codex
  worker reads mail between turns: "agree it with the other agent" had no
  bound either could see.

Each rule below names which of these it removes.

## Rule 1. One working tree per agent

Every agent works in its own working tree: a separate clone or a
`git worktree`, in its own workspace folder. No agent edits a tree another
agent also edits. The tree a runtime reads from is nobody's working tree.

Why: a branch does not separate files on disk. Two agents on two branches in
one directory still overwrite each other, and one still cannot commit without
carrying the other's half-finished work. This is the rule that removes the
first two failures above, on its own, before branches help at all.

Shape, the same on every host (spark1 already has it):

```text
~/workspaces/<agent>/           one agent, its own trees
    applications/               home tree per repository
    kdcube-ai-app/
    app-ecosystem/
    applications@w267/          a sibling tree for a second item in flight
```

On a machine that already holds shared checkouts (dev-main, under `~/src`),
the workspace is made of `git worktree`s of those repositories, not clones:
objects and refs are shared, nothing is duplicated, and a pushed branch is
visible from every tree. The home tree per repository stays detached at
`origin/main` between items and takes `git switch -c work/<wN>-<slug>` for an
item. A second item in flight gets a sibling tree named for it, removed when
its change request merges. Made real for fable-pub on 2026-09-22 22:12Z.

```bash
# once per repository, from the shared checkout, as yourself
git -C ~/src/kdcube/applications fetch origin
git -C ~/src/kdcube/applications worktree add --detach ~/workspaces/<agent>/applications origin/main
# a second item while the first waits on review
git -C ~/src/kdcube/applications worktree add ~/workspaces/<agent>/applications@w<N> -b work/w<N>-<slug> origin/main
# when its change request merges
git -C ~/src/kdcube/applications worktree remove ~/workspaces/<agent>/applications@w<N>
git -C ~/src/kdcube/applications branch -D work/w<N>-<slug>
```

Rules of the shape:

- **A worktree is not a temp directory.** A tree under a session scratchpad
  or `/private/tmp` dies with the session or the reboot and leaves a
  `prunable` entry behind. On 2026-09-22 the two shared repositories on
  dev-main carried about seventy registered worktrees from four agents, a
  dozen already `prunable`, none under a workspace (round 1, finding eight).
- **Remove your own tree when its change request merges**, with the branch.
  `git worktree prune` is yours for your own entries. The coordinator prunes
  only entries whose directory is gone, which touches nobody's work. A tree
  that still exists is its author's to remove, asked first (Rule 2, branch
  ownership).
- **Nobody edits the shared checkout any more**, not even to land: a merge
  lands on the integration ref, and the coordinator fast-forwards the shared
  checkout to it before any runtime action (step 6 of round 1). The landing
  steps in the worker skill remain for a machine that has no coordinator and
  no integrator yet.
- **A worktree cannot check out a branch another tree holds.** The shared
  checkout holds `main`, so a workspace tree is detached or on a work branch,
  which is what the rules want anyway.

The Problem Board client and relay run from one recorded source selection, not
from any working tree. `pb source use-release` selects an approved package
version and `pb source use-code` selects an exact App Ecosystem commit for
both command and relay. A direct checkout invocation is a visibly unpinned
development process and never changes that host selection.
The integration ref is source history, not a runtime selection.

A worktree cut for a review or a fix has no installed dependencies. Borrowing
the live checkout's `node_modules` by symlink works for third-party packages
and silently fails for the repository's own workspace packages: the
`@kdcube/*` links inside that `node_modules` point back into the live
checkout, so a worktree's `components-react` typechecks against the live
`components-core`, not the one on the branch. Seen on 2026-09-22 reviewing
KDCube #258, where new exports "did not exist". Make `node_modules` a
directory of links in the worktree and point the `@kdcube/*` entries at the
worktree's own `packages/`, then build the dependency package before the
dependent one. Python has no such trap: `PYTHONPATH` names the tree.

## Rule 2. Work on a branch, exchange through a change request

Each item is worked on a branch of the repository, pushed by its author, and
exchanged as a change request against the integration ref.

- **Branch:** `work/<wN>-<short-slug>`, one per item per repository, from the
  current integration ref. The item key first, so a branch names its work.
- **Push your own branch.** Operator ruling 2026-09-22: agents may push their
  own work branches and open change requests.
- **A branch belongs to the agent that pushed it.** The author deletes it when
  its change request merges, and deletes it themselves when they abandon it,
  so nobody else has to notice a stale branch. The coordinator deletes
  another agent's branch only when every commit on it is contained in the
  integration ref by patch identity (`git patch-id --stable`, shown), it has
  no open change request, and the author is told afterwards. Anything else is
  asked first. Why: on 2026-09-22 four abandoned branches were deleted for
  their author without asking, after the operator noticed them (round 1,
  finding four). Nothing was lost, and a shared action on someone else's
  work still needs their word or a shown proof.
- **Publishing is a release act.** A package version on an index is a
  release: the coordinator or the operator publishes it, from a merged
  integration ref (or, for a marker that claims a distribution name and
  carries no code, with the operator's agreement), and records on the item
  the version, the commit it was built from and the index it went to. A
  dependency floor that no published release meets is a release step with
  an owner, named in the merge order before the change request that needs
  it. Why: on 2026-09-22 a marker release made under the previous rules was
  found on the index by a reviewer with no record of who published it or
  from what (round 2, finding five), and a carve needed a release of
  another repository's package that no order had named (finding four).
- **The integration ref is the pushed `main`.** Operator ruling 2026-09-22
  20:36Z: the coordinator pushes `main` after merges, audited each time
  (names of the people and organizations we work with, the deployment host, co-author trailers), and deploying
  stays the operator's. A change request is expected to be reviewable only
  once the integration ref it targets is pushed: a reviewer reading 28 files
  instead of 13 lines wastes a review (round 1, finding one). Cut a branch
  from the pushed integration ref where you can. When the work depends on
  commits not yet pushed, the change request names the commit that is the
  scoped read.
- **One integrator per machine, elected once.** Operator, 2026-09-22: on a
  machine where no coordinator runs, the agents there elect one of themselves
  once as that machine's integrator, and the coordinator records who it is,
  per machine. Where a coordinator runs on the machine, it is the integrator.
  The integrator brings that machine's checkouts to the pushed integration
  ref before any runtime action there (the fast-forward in step 6), and runs
  the host-local actions the runtime-actions reference gives to "the agents
  on that host" (relay restart, procedure install). On a project with no
  coordinator at all, the elected integrator also pushes the integration ref
  after merges, and the project record names it.
- **Change request:** open it against the integration ref (`main` today) when
  the branch is ready for review, and put its link on the item and in the
  report. On GitHub a change request is a pull request. The board speaks of a
  change request so other hosting fits.
- **Review happens on the change request:** its diff, its base, its
  staleness. Mail may point at it and carries the verdict, but the change
  request is the thing reviewed. A review that names a local path is not a
  review, because the path is bound to one machine and one user.
- **The coordinator merges after approval.** Nobody merges their own change
  request. A merge advances the integration ref, from which runtimes release.
- **A change across several repositories** links every change request to the
  one item, states the merge order, and they merge together.
- **Public repositories** (kdcube-ai-app, app-ecosystem) take the change
  requests themselves, not forks. So the rule against naming the people and organizations we work with, and secret
  hygiene cover every branch name, commit message, change request description
  and review comment, not only what reaches the integration ref.

Why: a branch is portable where a local worktree path is not, so the same
rule holds on one machine or many. Real overlap then surfaces once, as a merge
conflict, resolved by the one reader who sees both sides, instead of as a block
before anyone can start.

Evidence it works, from today: codex-ui put its finished W253 slice on
`work/w253-resident-card-custody` and stopped being blocked by codex-main's
tree, and carved W255 on `work/w255-project-board-package` and
`work/w255-project-board-carve` without touching either `main`.

## Rule 3. Make your intent visible before you edit

Before the first edit for an item, publish what you are about to touch where
every agent can read it, and read what the others have published.

- Publish on the board's shared-write surface: `kind=source_in_flight`, the
  item key, and targets naming the repositories and the paths or areas you
  will change. Read the list first.
- If your targets overlap another agent's entry, do not start on the overlap.
  Send the overlap to the coordinator (Rule 4) and work the rest.
- Clear your entry when the change request is opened. The change request then
  carries the intent.

Why: two agents should not discover the same file at merge time. The surface
exists for exactly this and went unused all evening on 2026-09-22.

```bash
pb coordinate workspace.shared_write.list --object-ref <project-ref> --payload-json '{}'
pb coordinate workspace.shared_write.publish --object-ref <project-ref> --payload-json '{"kind":"source_in_flight","summary":"W245: review.return keeps the owner (store, dialog save, review-lifecycle doc)","targets":["repo:applications/playground/domain-solution/apps/problem-board@1-0/services/store.py","repo:applications/playground/domain-solution/apps/problem-board@1-0/services/work_item_edit.py"],"ttl_seconds":14400}'
pb coordinate workspace.shared_write.clear --object-ref <project-ref> --payload-json '{}'
```

## Rule 4. Where two changes meet, the coordinator decides

A decision that needs two agents' changes to agree goes to the coordinator,
with both sides named, and the coordinator decides. It does not go to the
other agent as a question.

Why: a peer cannot answer now. A Codex worker reads mail at the end of its
turn, so a question to it has no bound the asker can see, and "currently not
listening" is normal operation, not a fault. The coordinator is the one
participant whose job is to be the decision point rather than a message in a
queue.

What the coordinator decides: merge order, who owns a shared file for this
round, whether one change waits for the other, and how a conflict resolves.
The decision goes on the item as a note, so both agents read the same words.

**When you are blocked behind a peer that cannot answer now:** report the
block to the coordinator with both sides named, take the next item of yours
that touches none of the same paths, and say on the blocked item what you are
waiting for. Do not wait silently and do not work the overlap. codex-ui did
this twice on 2026-09-22 and both times the work kept moving.

## Rule 5. The merge gate

The coordinator merges a change request when all of these hold, and refuses
it naming the one that does not:

1. **Approved** by the named reviewer, and the reviewer is not its author.
   The approval is a board mail naming the head, quoted on the change request
   as a comment pinned to that head. It is not a platform approval: every
   agent pushes with the operator's account, so GitHub sees one author and
   refuses the approval as the author's own (round 1, finding five). The
   change request is the diff surface, the board is the review record. If
   the team grows, a GitHub identity per agent is the target, a cost the
   operator decides.
2. **Base current:** rebased or merged onto the current integration ref, so
   the tested tree is the tree that lands. The author checks this before
   asking for a review (`git merge-base --is-ancestor origin/main <head>`)
   and again after every integration push, which moves the base under every
   open change request. A rebase is pushed with `--force-with-lease` on the
   author's own branch and the new head is named on the change request.
3. **Suites green on the branch head**, both the Python and the widget suites
   where a widget changed, run by the author and stated with counts, rerun on
   every new head. A regression written for a review finding is shown to fail
   on the head without the fix (a monkeypatch or a reverted hunk is enough),
   because a test that passes either way proves nothing about the finding.
   A fake used in that proof models every constraint the proof depends on
   (a primary key, a unique rule, a fenced update), or the proof runs against
   the real database. Why: on 2026-09-22 a regression over a fake without the
   event table's primary key passed a citation that collided live (round 1,
   finding six).
   The reviewer's counterpart: a proof covers the path the finding is about.
   A suite that never puts two entry points in one test proves nothing
   about their interaction (round 1, finding seven: the side-server drain
   guard was tested side against side, and the cycle path bypassed it).
4. **Runtime import path stated and proven** when the change alters what a
   deployed runtime imports (a moved module, a renamed package, a new
   dependency): the change request says how the runtime gets it (which install
   step, which refresh or reload, on which host) and shows it ran. Why:
   codex-ui's `project_board.contract` carve passed every test on the host and
   would have broken the container at the next reload.
   The host relay environment is a runtime too: a `pb` and relay that run
   from a checkout stop at the fast-forward when that checkout starts
   importing a package their environment lacks, so the host step (install
   the release, confirm the source mode, restart) runs before the
   fast-forward, on every host, and the change request shows it did (round
   2, finding three).
5. **Every place that enforces the rule is listed and checked.** A change that
   fixes a rule names each place the rule is enforced (server, client, other
   operations, docs) and says what was done at each. Why: W245's rule sat in
   the server, the widget draft reducer and `review.return`, and each fix
   found the next. A test per fixed place proves the fixed places and nothing
   about the place that was not listed.
6. **Docs move with behaviour**, in the same change request, one home per
   concept, no link to a gitignored path.
7. **Nothing that should not be public** in the branch, the commits, the
   description or the comments, for a public repository.

After the merge the coordinator names the merged ref on the item. A runtime
action releases that ref, never a working tree (see
`problem-board-worker/references/runtime-actions.md`).

## Rule 6. Your visible state says where you are and what you are on

At any moment another agent, the coordinator or the operator can read, from
the board alone: which item you hold, which branch and change request carry
it, what you touched (Rule 3), and what you are waiting on.

- Report `working` against your assignment when you start, with the branch
  name once it exists.
- Put the change request link on the item when you open it.
- When you wait on a review or a decision, say so on the item, with the
  correlation you wait for, instead of waiting silently.
- Report `completed` only when the change request is merged, or when the item
  says otherwise, with the merged ref and the evidence you ran. Each report
  cites as its source event the event that prompted it: the assignment notice
  for the first, and for a later one the later mail that carried the news,
  the merge notice for a completion when earlier progress reports spent the
  assignment notice (W267, 2026-09-22 22:17Z). A source event is spent once.
- **When a review returns your item**, you keep it: the assignment stays
  yours with a new ownership version, the branch stays, the change request
  stays open, and your next report cites the returned-work notice as its
  source event, never the review, which the return itself already spent
  (W245 blocker three, refused live as a duplicate event until the notice
  said what to cite). Push the rework to the same branch.

Why: the operator's measure for this procedure includes "their info reflects
where they are and what they work on". A status that lags reality is a
collision waiting to happen, because someone plans against it.

## From this moment: round 2 on dev-main, 2026-09-22 22:20Z

Round 1 ran four hours under rules that did not exist when it started, and
changed four of them from eight findings. Round 2 runs under the rules as
they stand now. What every agent on dev-main does:

1. **Work from your own workspace.** `~/workspaces/<agent>/<repo>`, a
   worktree of the shared checkout (Rule 1 has the commands). Make yours
   before your next edit, nobody makes another agent's. A second item in
   flight gets a sibling tree, removed with its branch when the change
   request merges.
2. **Nobody edits the shared checkouts under `~/src`.** Not to develop, not
   to land. A merge lands on the integration ref, and claude-main
   fast-forwards the shared checkouts to it before any runtime action.
3. **Every item on a branch, every branch pushed, every review on a change
   request** at a named immutable head, with the link on the item and in the
   report. Delete your branch when it merges or you abandon it.
4. **Intent before the first edit**, `source_in_flight` with the item key and
   the paths, read first, cleared when the change request opens. Overlap
   goes to claude-main, on the item, both sides named.
5. **Before you ask for a review**, the author's side of the gate: base check
   (`git merge-base --is-ancestor origin/main <head>`), suites on that head
   with counts, a regression shown to fail without the fix, every enforcing
   place listed, docs in the same change request, nothing non-public.
6. **Approval is a board mail naming the head, quoted on the change request
   pinned to it.** The reviewer proves the path the finding is about, over a
   fake that models the constraints the proof depends on, or live.
7. **claude-main merges, pushes the integration ref, names the merged ref on
   the item, installs procedure revisions, and runs reloads and relay
   restarts** after collecting ready from every agent on the host.
8. **Acceptance is the behaviour observed live**, not the suites on the
   branch: the coordinator verifies the way the operator would, after the
   runtime action that makes the merge live.
9. **Keep your visible state true**, and **every collision, block, stale
   read or rule that did not fit gets a Round 2 entry** in the log below,
   written by whoever saw it, as it happened. Mail fable-pub or note it on
   W262.

## Interim, 2026-09-22 22:20Z: what is true now versus the target

| target | dev-main now | spark1 now |
| --- | --- | --- |
| one working tree per agent | fable-pub in its workspace, three agents still in the shared checkouts until they make theirs | one workspace per agent, own clones |
| branches and change requests | every item since 20:10Z, twelve change requests merged or open | from the first task |
| pb runs from a pinned copy | selected package version or commit, reported by `pb source status`; the shared checkout is not a runtime | selected package version or commit, reported by `pb source status` |
| coordinator merges and pushes | claude-main, on the host | claude-main, remote |
| review record | board mail plus a pinned change request comment, one GitHub account for all | same |

Nobody lands by hand any more on dev-main. The landing steps in the worker
skill remain for a machine with neither a coordinator nor an elected
integrator.

## Rehearsal log

One entry per round. Record what collided, who was blocked, what nobody could
see, and what changed in this procedure because of it. Observations, not
intentions.

### Round 0, 2026-09-22 (before the procedure)

- Collision: codex-ui's W253 commit blocked by codex-main's uncommitted W272
  files in the shared app-ecosystem checkout. Resolved by codex-ui moving to
  `work/w253-resident-card-custody`. Rule 1 and Rule 2.
- Misattribution: fable-pub reported codex-main's uncommitted
  `add-a-worker-host.md` as claude-main's, during a reload readiness check.
  Rule 1 and Rule 3.
- Near miss: a carve that passed on the host would have broken the container
  at reload. Rule 5, gate 4.
- Rule in three places: W245 fixed in the server, then the widget reducer,
  then `review.return`. Rule 5, gate 5.
- Unbounded wait: an overlap between two workers could not be settled peer to
  peer. Rule 4.

### Round 1, from 2026-09-22 20:10Z

Subjects: claude-main (coordinator), codex-main, codex-ui, fable-pub, on
dev-main. Entries are added as they happen.

- **20:30Z, finding one: a change request needs a pushed integration ref.**
  The first two change requests (applications #4 and #5) were cut from local
  `main`, one commit each. GitHub diffed them against `origin/main`, which was
  14 commits behind local `main` because pushing `main` is the operator's act
  and nobody had pushed. Both change requests therefore showed 13 commits and
  28 files of other agents' work, and the reviewer refused #4 on scope
  (correctly, under Rule 2). Rebasing onto `origin/main` was no fix: the
  item's own earlier commits were among the unpushed ones. Resolution asked
  for: the operator pushes `main` before or at the start of a round, or the
  procedure names who may push the integration ref. **Resolved 20:36Z:** the
  coordinator pushes the integration ref after merges (operator ruling), and
  applications `main` was pushed, d34b3d79 to 450bc027, 15 commits, audited.
  Both change requests then showed their one commit. Rule 2 gained the pushed
  integration ref, the scoped-read line, and the per-machine integrator.
- **20:56Z, finding two: the review of the change request found the fourth
  place.** codex-main's review of applications #4 read `services/store.py`
  around the return and found that the version advanced and the control was
  issued, but the ownership ledger the report path authorizes against got no
  row: the worker the return woke would have reported into
  `work_assignment_stale` on its first rework report. Two mail reviews of the
  same code had passed it. The reviewer applied gate 5 (every enforcing place)
  and asked for a store-contract regression that reports under the returned
  version, not an in-memory control test. The fix landed as head `b708aca0`
  with the regression, which fails with `KeyError: 2` when the insert is
  removed. Rule 5 gate 3 gained the line that a regression for a finding is
  shown to fail without the fix. No rule changed otherwise: the gate worked.
- **21:00Z, finding three: both heads fell behind the integration ref after
  the integration push.** `origin/main` moved to `f7329086` at 20:36Z (the
  finding-one push) and the two change request heads had been cut before it.
  The reviewer's base check (gate 2) refused #4 on that as well. Both branches
  were rebased in their worktrees (`--force-with-lease` on the author's own
  branch), the full Python suite was rerun on each new head (#4: 1203 passed,
  #5: 1208 passed), and the new heads were named on the change requests and
  by mail. Gate 2 gained the author's side: check `git merge-base
  --is-ancestor origin/main <head>` before asking for a review, and rerun the
  suites on the rebased head, because every integration push moves the base
  under every open change request.
- **21:05Z, finding four: the coordinator deleted another agent's branches
  without asking** (recorded by claude-main on W262). The operator saw two
  branches in the app-ecosystem pull-request view and asked whether they had
  been handled. They were codex-main's abandoned duplicates from 20:35Z with
  no change request. claude-main verified every commit was patch-identical to
  one already on `main` (`git patch-id --stable` per pair), then deleted all
  four, two per repository, without asking their author. Nothing was lost,
  and that is not the point: a branch belongs to the agent that pushed it.
  Rule 2 gained branch ownership: the author deletes their own branch when its
  change request merges or when they abandon it, and the coordinator deletes
  one only when its commits are all contained in the integration ref by patch
  identity (shown), it has no open change request, and it tells the author
  afterwards. Anything else is asked first.
- **21:16Z, finding five: GitHub cannot tell the agents apart** (recorded by
  claude-main on W262). claude-main tried to approve change request #6 and
  GitHub refused: "Can not approve your own pull request". Every agent commits
  and pushes with the operator's account, so on GitHub there is one author,
  one reviewer, one identity, and gate 1 cannot be enforced on the platform.
  Interim, in the gate: the approval is recorded on the board (mail with the
  head named) and quoted on the change request as a comment pinned to that
  head. Target, if the team grows: a GitHub identity per agent, a cost the
  operator decides.
- **21:21Z, finding six: the fake hid what the live proof found.** The
  regression written for finding two ran the return and the rework report
  over a fake connection with an ownership ledger and no event table. The
  rework report cited the review ref, as the notice told it to, and passed,
  because the fake had no primary key on `problem_board_service_events`. Live,
  the same citation collided with the `review.return` event that already
  owned that id, and the W212 report failed on every retry with a bare
  unique-violation error (codex-main's chat-proc evidence, 21:16Z to 21:18Z).
  Fixed on #4 head `c21109f2`: the notice tells the worker to cite the notice
  itself, the store refuses a spent source event as `work_source_event_taken`
  naming what to cite, and the fake models the primary key. Gate 3 gained the
  fidelity line: a fake used to prove a finding models every constraint the
  proof depends on, or the proof runs against the real database. The
  underlying identity inconsistency (event id from the source event alone,
  replay rule per actor) was filed as its own item, W273, rather than
  widening W245. Rule 4 in use, 21:34Z: codex-main proposed citing the
  returned-work control, fable-pub implemented citing the notice, and
  claude-main decided for the notice, naming both sides and the three reasons
  (one citation rule for workers, the notice is one per version, the control
  ref is relay-internal), instead of leaving the two to agree mid-turn.
- **21:51Z, finding seven: a suite that never puts two entry points in one
  test proves nothing about their interaction** (codex-main's review 5 of #5).
  The side-server drain guard was tested side against side, and the cycle's
  own drain bypassed it, so a blocked side drain and a later cycle drain
  overlapped for one worker. The reproducer failed on the head in 0.21 s. Gate
  3 gained the reviewer's counterpart: a proof covers the path the finding is
  about. Fixed on #5 head `e47b9bf7` with one per-worker lock under both
  paths and the side-blocked versus later-cycle regression.
- **22:12Z, finding eight: worktrees accumulate where nobody owns them.**
  Making Rule 1 real for fable-pub, `git worktree list` on the two shared
  repositories showed about seventy registered trees from four agents under
  `/private/tmp`, session scratchpads and `.git/codex-work`, a dozen
  `prunable`, none under a workspace. fable-pub's own review and fix trees
  were among them. Rule 1 gained the shape as worktrees of the shared
  repositories under `~/workspaces/<agent>/`, the sibling tree for a second
  item, removal on merge, and who may prune what. fable-pub's workspace exists
  as of this entry, with W267 moved into it. The other three agents make
  theirs, each their own (Rule 1: nobody makes another agent's tree).

### Round 2, from 2026-09-22 22:20Z

Subjects: claude-main (coordinator), codex-main, codex-ui, fable-pub, on
dev-main, under the rules as revised by round 1. The question this round
answers: what breaks under rules that are four hours old, once every agent
works from its own tree. Entries are added as they happen.

- **22:20Z, opened.** State at the open: change requests #4 to #10 merged
  (W245, W267, three procedure revisions, one journal), #12 open (W267's
  journal), procedure package 2026.09.22.9 installed for both runtimes,
  fable-pub's workspace made, the other three agents' workspaces pending, a
  problem-board reload and a relay restart announced and waiting on ready
  from codex-main and codex-ui, W245 and W267 awaiting live acceptance.
- **22:19Z, finding one: the coordinator hits the mid-turn gap too.** Twice
  in half an hour claude-main's merge notice and fable-pub's "ready for your
  merge" mail crossed (#5 was merged while the approval was being forwarded,
  #10 while its final head was being named). Nothing was lost, because the
  change request is the shared state both were reading, not the mail. The
  rule that follows, already in Rule 6: read the item and the change request
  before acting on a mail about them, and a message that arrives after the
  thing it belonged to is finished arrives nowhere. Recorded by claude-main,
  who saw it from the other side.
- **23:19Z, finding two: a carve rewrote the living document it moved.**
  Reviewing W255, the package that moves the worker procedure into its own
  distribution shipped a rewrite of this procedure (585 lines different from
  the installed `.10`): the rehearsal log, the operator's one-integrator
  ruling, Rule 1's shape and Rule 6's corrected citation were gone, and its
  ledger reused revision numbers for different content. After the carve,
  every install would have regressed silently. claude-main decided (Rule 4):
  the package carries the installed revision verbatim and adds its own
  guidance as a new revision with a ledger line. The rule it confirms: a
  move carries the document's current revision and ledger, and a change to
  its rules is a change request on the document, not a side effect of a
  move (Rule 2, gate 6).
- **23:19Z, finding three: the host is a runtime the gate forgot.** The same
  carve made the shared checkout's `pb` entry import the new package while
  the relay venv on dev-main did not have it. The fast-forward after the
  merge would have stopped `pb` and the relay here before any image was
  touched. Gate 4 gained the host clause and claude-main schedules the host
  step before the fast-forward, per host.
- **23:19Z, finding four: an install chain with an unnamed link.** The
  package required a CLI that was not on the index, which required another
  repository's CLI at a version never published. The stated merge order
  named two of the three publications and not the one from the other
  repository, the operator's act. Rule 2 gained the release bullet: a floor
  no release meets is a release step with an owner, in the order.
- **23:20Z, finding five: a release made under the previous rules is still a
  release** (recorded by claude-main). `project-board 2026.9.22.2100` on the
  public index was a planning marker claiming the distribution name, built
  from app-ecosystem `7ff688b` on `main`, published by claude-main with the
  operator's agreement before the change-request model applied to that
  repository. The reviewer found it with no record on any item. Rule 2 now
  says who may publish, from which ref, and what is recorded.
