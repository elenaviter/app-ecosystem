---
id: project-board.skill-reference.collaboration
title: Collaborate Across Agents And Machines
summary: How several agents on several machines work on the same repositories without blocking or overwriting each other, exchange work through change requests, and integrate through the coordinator. Written to be run, observed and revised, one rehearsal round at a time.
tags: [procedure, problem-board, collaboration, git, change-request, coordinator, multi-machine]
keywords: [one tree per agent, work branch, change request, pull request, merge gate, integration ref, intent before edit, shared write, coordinator decides, rehearsal round, collision log]
see_also:
  - ./coordinator.md
  - ./runtime-actions.md
  - repo:app-ecosystem/products/project-board/packages/project-board/src/project_board/procedures/agent-worker.md
  - repo:app-ecosystem/products/project-board/packages/project-board/src/project_board/procedures/operator.md
  - repo:app-ecosystem/products/project-board/packages/project-board/src/project_board/procedures/add-a-worker-host.md
---

# Collaborate Across Agents And Machines

**Status:** first pass, 2026-09-22 (W262). Usable, not complete. It is a
loop, not a document: the team works under it, what collides is recorded in
the project's journal, and the rules change on what was observed. The measure is the count of collisions and mistakes going down from
one round to the next.

**Written for:** several agents on several machines: a shared host where
several agents work beside the runtime, and further hosts added with
`procedures/add-a-worker-host.md`. Nothing below assumes one checkout, one
host, or that two agents can talk to each other in real time. One machine is a
fair test of every rule except the cross-host paths, so the loop starts on the
shared host before a second host is authorized.

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

## Rule 1. One working tree per agent

Every agent works in its own working tree: a separate clone or a
`git worktree`, in its own workspace folder. No agent edits a tree another
agent also edits. The tree a runtime reads from is nobody's working tree.

Why: a branch does not separate files on disk. Two agents on two branches in
one directory still overwrite each other, and one still cannot commit without
carrying the other's half-finished work. This is the rule that removes the
first two failures above, on its own, before branches help at all.

Shape, the same on every host. The root is `~/.kdcube/pb/workspaces/<alias>`
(operator ruling, 2026-09-25: not in the user's home folder); a host set up
before the ruling keeps its old folders until a planned move:

```text
~/.kdcube/pb/workspaces/<alias>/   one agent, its own trees
    <repo>/                        home tree per repository
    kdcube-ai-app/
    app-ecosystem/
    <repo>@w<N>/                   a sibling tree for a second item in flight
```

On a machine that already holds shared checkouts (for example under `~/src`),
the workspace is made of `git worktree`s of those repositories, not clones:
objects and refs are shared, nothing is duplicated, and a pushed branch is
visible from every tree. The home tree per repository stays detached at
`origin/main` between items and takes `git switch -c work/<wN>-<slug>` for an
item. A second item in flight gets a sibling tree named for it, removed when
its change request merges.

```bash
# once per repository, from the shared checkout, as yourself
git -C ~/src/<repo> fetch origin
git -C ~/src/<repo> worktree add --detach ~/.kdcube/pb/workspaces/<alias>/<repo> origin/main
# a second item while the first waits on review
git -C ~/src/<repo> worktree add ~/.kdcube/pb/workspaces/<alias>/<repo>@w<N> -b work/w<N>-<slug> origin/main
# when its change request merges
git -C ~/src/<repo> worktree remove ~/.kdcube/pb/workspaces/<alias>/<repo>@w<N>
git -C ~/src/<repo> branch -D work/w<N>-<slug>
```

Rules of the shape:

- **A worktree is not a temp directory.** A tree under a session scratchpad
  or `/private/tmp` dies with the session or the reboot and leaves a
  `prunable` entry behind. On 2026-09-22 the two shared repositories on
  the shared host carried about seventy registered worktrees from four agents, a
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
version and `pb source use-code` selects an exact App Ecosystem commit as one
release for both command and relay. A direct checkout invocation is a
visibly unpinned development process and never changes that host selection.
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
- **Push at every coherent checkpoint.** A checkpoint is a recoverable state
  with its next step explicit, not every command and not every passing
  test. A WIP commit is a checkpoint when it says so and names what still
  fails. Local-only work is never a handoff: a worker on another machine can
  read a pushed branch and nothing else. Team decision 2026-09-23 (P1, four
  yes votes): every other visibility rule depends on this one. A WIP push to
  a public repository gets the same check as a final one (Rule 9).
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
  staleness. The change request is the thing reviewed, and the verdict also
  goes to the author by board mail (the head, the verdict, the link), in the
  same step as the review comment. Why: a verdict posted only as a review
  comment reaches nobody on the board; on 2026-09-26 a request for one test sat
  unseen on a board change request for 1 h 45 min. A review that names a local path is not a
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

Evidence it works, from 2026-09-22: one agent put its finished W253 slice on
`work/w253-resident-card-custody` and stopped being blocked by another's
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
- **Say your scope in the `working` report, and declare your worktree once.**
  Team decision 2026-09-23 (P4b, P4a). The first `working` report carries one
  optional line, `--scope`, naming the module, path prefixes or runtime
  surface you will change, set again only when the boundary grows. Then
  declare where on this host you edit each repository the assignment binds,
  once, so the relay publishes the tracked files you have in flight:

  ```bash
  pb worker report --state working --scope 'client/relay.py, services/control.py heartbeat' ...
  pb worker workspace --assignment-ref <assignment-ref> --repository repo:app-ecosystem/products --path ~/.kdcube/pb/workspaces/me/ae@w278b
  pb worker workspace --clear --assignment-ref <assignment-ref>
  ```

  The board then shows, under each repository of your assignment, the tracked
  paths that changed since the base commit or are modified now, republished
  when the set changes, and `no worktree declared` until you declare. Tracked
  paths only, never untracked names or contents (Rule 9). Observed files are a
  signal for a teammate deciding where to start. They are not the handoff and
  not the contract, the pushed branch is (Rule 2), and the scope line is your
  word before the first edit.

Why: two agents should not discover the same file at merge time. The surface
exists for exactly this and went unused all evening on 2026-09-22.

The three operations, with a real publish payload (`kind`, a `summary` that
starts with the item key, `targets` as repository paths, a `ttl_seconds` that
is recovery for an abandoned entry and not the completion path):

```bash
pb coordinate workspace.shared_write.list --object-ref <project-ref> --payload-json '{}'
pb coordinate workspace.shared_write.publish --object-ref <project-ref> --payload-json '{"kind":"source_in_flight","summary":"W<N>: <what changes, in a few words>","targets":["repo:<repo>/<path/to/changed/file>"],"ttl_seconds":14400}'
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
waiting for. Do not wait silently and do not work the overlap. One agent did
this twice on 2026-09-22 and both times the work kept moving.

## Rule 5. The merge gate

The coordinator merges a change request when all of these hold, and refuses
it naming the one that does not. A gate names what a reader does to satisfy
it, with a pointer to the means, or it is not a gate yet: gate 3's merger
clause and finding ten's stamp rule were both written without the thing that
makes them followable, and only the rule got reviewed (round 2, finding
fifteen). A verification that ends by running the suite it just made runnable
is a verification. One that stops at printing JSON is a report.

1. **Approved** by the named reviewer, and the reviewer is not its author.
   The approval is a board mail naming the head, quoted on the change request
   as a comment pinned to that head. It is not a platform approval: every
   agent pushes with the operator's account, so GitHub sees one author and
   refuses the approval as the author's own (round 1, finding five). The
   change request is the diff surface, the board is the review record. If
   the team grows, a GitHub identity per agent is the target, a cost the
   operator decides. When the merger has reviewed the change request itself
   and merges, it tells every other reviewer it invited at that moment, so
   nobody spends a turn reviewing something already landed (round 2,
   finding eight).
   The approval states two counts and reconciles them: the files the change
   request lists and the files the reviewer read, the way the skill reads
   `item_count` against `items[]` for mail. A truncated listing never means
   the omitted files do not exist. Why: on 2026-09-23 a delta listing was cut
   to four of five files at bf25ed1 and the approval covered an unread file
   (round 2, finding fourteen). It also states the inputs of every suite run
   it cites (the interpreter, the dependencies its environment holds, the
   overlays, any database or variable a test needs), so two counts from two
   environments can be compared at all (finding seventeen).
2. **Base current:** rebased or merged onto the current integration ref, so
   the tested tree is the tree that lands. The author checks this before
   asking for a review (`git merge-base --is-ancestor origin/main <head>`)
   and again after every integration push, which moves the base under every
   open change request. A rebase is pushed with `--force-with-lease` on the
   author's own branch and the new head is named on the change request.
   The merger may instead test the exact merged tree of heads that are
   behind, and prove after merging that `main`'s tree equals it
   (`coordinator.md`, Merge).
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
   The counts are what the tool said, not what the shell returned: the
   report carries pytest's own summary line verbatim for each suite, from
   the run at that head, and the author checks pytest's exit status, never
   a pipeline's. Why: on 2026-09-23 a chain read `grep`'s status over a
   failing pytest and pushed 4a8f487b as green (round 2, finding twelve).
   The merger's counterpart: a reported count is a claim about evidence, so
   the merger runs the suites on the exact head before merging. The command,
   its interpreter and the source overlays it needs are written once, in the
   application's `procedures/testing.md`, section Run The Package Suite. Do
   not rediscover them: a bare interpreter or the relay's fails on imports
   that read like regressions and are not. That section keeps checkout
   variables, not machine paths, so it reads the same on every host: fill
   them from your own machine. A placeholder is not a missing value.
   The second counterpart, for reviewer and merger alike: compare your count
   with the author's and ask about the difference. A skipped test names its
   missing input, and a suite that skips what the change touches is green
   about everything except the thing under review. Why: on 2026-09-23 the
   merger's 663 against the author's 667 at 0518f213 were the four PostgreSQL
   tests covering the change, skipping on an unset DSN (round 2, finding
   seventeen). When the change adds a dependency, one of the runs comes from
   an environment holding only what the packages declare: a venv with more
   than the declared set hides a missing declaration, one with exactly it
   does not. Before any run, the dependency preflight in
   `procedures/testing.md` names the first declared dependency the
   interpreter lacks in one sentence, because a missing one surfaces as
   collection errors that look nothing like the cause.
   A change to a contract another repository's code or tests exercise (the
   client the board app loads through its `local/` aliases, a payload, a
   config or a file shape both read) runs that repository's suite against the
   change's head too, author and reviewer both, before approval, and the
   counts name both repositories. On a head that edits the worker procedure
   package, `test_package_content_is_recorded_for_its_revision` skips on an
   author's head with the reason "procedure changed; the merger sets the
   revision", because authors never bump it (`coordinator.md`, Merge); the
   author names that skip with the others. Why: on 2026-09-26 app-ecosystem#208 moved
   journal resolution to each worker's own clone, its reviews ran only the
   package suite, and eight board tests failed on main (W352).
4. **Runtime import path stated and proven** when the change alters what a
   deployed runtime imports (a moved module, a renamed package, a new
   dependency): the change request says how the runtime gets it (which install
   step, which refresh or reload, on which host) and shows it ran. Why:
   the `project_board.contract` carve passed every test on the host and
   would have broken the container at the next reload.
   The host relay environment is a runtime too: a `pb` and relay that run
   from a checkout stop at the fast-forward when that checkout starts
   importing a package their environment lacks, so the host step (install
   the release, confirm the source mode, restart) runs before the
   fast-forward, on every host, and the change request shows it did (round
   2, finding three).
   A claim about what a host installs, or what a dependency closure contains,
   is settled by installing it into a fresh environment at the named commit.
   Reading a `pyproject.toml` forms the claim and does not settle it, and
   "installs from X only" without a run is a hypothesis. Why: the environment
   you already have is the one that hides the answer, the closure sits on
   `PYTHONPATH` as paths you stop seeing (round 2, finding thirteen).
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
8. **A skill change fits by moving, not by compressing.** `SKILL.md` holds
   fewer than 520 newlines, enforced by `test_skill_carries_rules_not_stories`.
   Why: every worker session loads the skill into context, so the operator
   ruled on 2026-09-18 that it carries rules with one clause of reason and
   nothing else (revision 2026.09.18.8 took it from 874 to 498 lines). What
   follows: content that does not fit moves to a reference opened by one
   trigger line in the skill, and existing prose is not tightened to make
   room. Tightening reads as free and is not: the small facts go first, and
   they were the expensive ones to learn (finding eleven). New prose is
   wrapped like its neighbours, so the count is honest. The reviewer diffs
   the skill for facts that left, not only for lines that arrived.

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
  correlation you wait for and the actor or event that clears it, instead of
  waiting silently. A blocker that names no one is a status label and cannot
  drive anyone's next decision (P11, 2026-09-23).
- **Publish at transitions, not on a clock.** Claim, checkpoint pushed,
  blocked, review opened, merged, handoff: each is one statement. A heartbeat
  that repeats an unchanged state is prose nobody reads. Before a disruptive
  operation (an apply, a migration, a runtime window) the transition also
  names what was preflighted before the point of no return, because that is
  the fact a remote worker cannot check afterwards (P5, 2026-09-23, from the
  W253 apply whose schema defect surfaced only after the writers stopped).
- **A resume record on the item.** At each coherent checkpoint and at a
  handoff, one note on the item carries what nothing else holds: the
  decisions taken with their refs, the assumptions rejected, the tests run
  with the command and interpreter, the next concrete step, the blockers
  with who clears them, and what is unverified. It does not repeat the
  branch, base, latest commit or change request, which the assignment and the
  `working` report carry, and it is rewritten at checkpoints, not after every
  command. A successor reads it before its first edit (Rule 8). Team
  decision 2026-09-23 (P2, four yes votes after the derived-versus-typed
  split was drawn).
- Report `completed` only when the change request is merged, or when the item
  says otherwise, with the merged ref and the evidence you ran. The report
  names a commit and the integration ref that contains it, the merge commit
  on the pushed `main`, and you have fetched and run `git merge-base
  --is-ancestor <commit> origin/main` first, not before the merge and not
  from memory. The acceptor runs the same command on their own clone before
  accepting, because the report is a claim and the clone is the evidence. A
  journal entry that says landed names that merge commit and is written
  after it is fetched, never from the intention to merge (finding eighteen:
  three reviewed commits journaled as landed sat in no branch for two days).
  Each report cites as its source event the event that prompted it: the assignment notice
  for the first, and for a later one the later mail that carried the news,
  the merge notice for a completion when earlier progress reports spent the
  assignment notice (W267, 2026-09-22 22:17Z). A source event is spent once.
- **When a review returns your item**, you keep it: the assignment stays
  yours with a new ownership version, the branch stays, the change request
  stays open, and your next report cites the returned-work notice as its
  source event, never the review, which the return itself already spent
  (W245 blocker three, refused live as a duplicate event until the notice
  said what to cite). Push the rework to the same branch.

- **In review, the reviewer is who must act** (W326, operator 2026-09-25).
  An item always has an assignee and a status, and the assignee is who must
  act now: the worker while it is Working, **the reviewer** while it is in
  Review. There is no new status. Name the reviewer on the completed report
  with `--reviewer <stable worker name>` or `--reviewer operator`; with none
  named, the acting coordinator reviews and routes it (`review.assign`). The
  operator is named only once the work is integrated: `--merged <commits>`
  and `--deploy "<window>: <check>"`, or `--nothing-to-deploy`; otherwise the
  report is refused with `work_review_operator_evidence_missing`, naming what
  is missing, and nothing is applied. The item keeps you as its assignee
  ("worked by"), so a return comes back to you. Why: the operator's review
  list held every item in Review, most with nothing for the operator to look
  at (W314, W287, W300 on 2026-09-25); a list of what is really the
  operator's needs a named reviewer on every item, and work reaches the
  operator only when it is merged and deployed.

Why: the operator's measure for this procedure includes "their info reflects
where they are and what they work on". A status that lags reality is a
collision waiting to happen, because someone plans against it.

### Until when: the worker's estimate, 2026-09-23

Nobody on a project could see until when a worker expected to finish what it
was on, so a coordinator waiting on a change request read silence and an
overrun the same way. The estimate is the worker's own statement, kept in its
worker record, shown on its card, and marked overdue by the board once the
time has passed without a new value or a clear:

```bash
pb worker busy-until 2026-09-23T21:30Z --note 'W262: estimate command, procedure, tests'
pb worker busy-until 2026-09-24T09:00Z --note 'W262: slipped, the widget test harness needs a clock'
pb worker busy-until --clear
```

The rule has three moments. Set it after planning, when the work is
understood well enough to name an end. Set it again, with the reason in the
note, the moment it slips: an overdue estimate that nobody re-set says the
worker is not watching its own clock. Clear it when the work is done, so an
idle worker shows no stale promise. The time is UTC and the board refuses any
other zone, so every reader compares the same instant.

The estimate is coarse and it is enough. Nontrivial work gets one after
planning, a brief action gets none, and no value pretends to a precision the
worker cannot justify: the operator decided on 2026-09-23 that there is no
confidence value beside it, the time, the note, its age and the reason for a
slip say what a reader needs. The board shows the age and marks an overdue
estimate apart from a blocked state, because stale and blocked call for
different actions (P8, four yes votes).

### The info line

When the operator tells you something the team must know about you (for
example, not to be used actively, or reviews only), publish it:

```bash
pb worker info "<one line, at most 200 characters>"
pb worker info --clear
```

Clear it the moment it no longer holds. Why: the line is first on every card
of yours and in the team of `pb worker context`, where the operator and the
coordinator look before routing (coordinator, Route, step 0), while mail about
it reaches only whoever reads that mail (W330, operator, 2026-09-25). The
line rides the relay's next heartbeat, which every worker Card already holds,
so it shows within about two minutes (the idle heartbeat ceiling); `pb worker
info` without arguments says `on_board = True` once the board has it.

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

## Rule 8. Handoff is an ownership decision, not a note

Work moves from one worker to another only by a new ownership version on the
assignment, and the coordinator issues it. Three things start a handoff: the
predecessor publishes a checkpoint (Rule 2) and says it is done or cannot
continue, the relay reports the predecessor out of tokens or rate limited
(W26, the runtime's own word, never inferred from silence), or the estimate is
overdue with no pushed checkpoint since it was set. In each case the
coordinator decides: wait for the reset, or reassign. It does not happen by
itself, because a limited or silent worker may still hold uncommitted state
that a reassignment would orphan.

The successor accepts the checkpoint by taking the new ownership version and
reads the resume record (Rule 6) before its first edit. From that moment the
predecessor cannot report or mutate under the old version: the board refuses
a stale version (`work_assignment_version_conflict`), which is the fence that
keeps two workers from both believing they own the item. A predecessor that
comes back after its reset reads the item first, like anyone else.

Rule 8 covers work items. The coordinator role itself moves by
[coordinator](coordinator.md), Hand the coordinator role over, and take it back.

Why: two members proposed this independently on 2026-09-23 (an atomic
ownership handoff, and a handoff as a decision with an owner), and all four
adopted the merged form (P12). The ownership version already existed as the
fence for reports. This gives it a trigger and a decider.

## Rule 9. What is published is safe to publish

Everything a worker publishes for another reader (a status, a resume record,
a handoff, observed paths, a WIP push) carries stable refs, hashes and
redacted evidence, and never a secret, a bearer credential, the name of an
untracked file, machine-private data, or the name of a person or organization
we work with. A WIP push to a public repository (kdcube-ai-app,
app-ecosystem) gets the same check as a final push, and observed files in
flight are tracked paths only. Why: visibility that leaks is worse than none,
and the checks that exist for a final change request (Rule 2) were never
meant to be skipped by pushing earlier (P13, 2026-09-23, four yes votes).

## Rule 10. A runtime window speaks one channel that survives it

While the platform is down for an apply or a migration, the board is
unreachable, so nothing said during the window reaches a worker on another
machine. The announcement before the window therefore names each step's owner,
the rollback trigger, and the point after which nothing is expected from
remote workers. The all-clear on the board is the only resume signal, and
every result produced during the window is posted to the board after it. A
file on the host that ran the window is not a channel: on 2026-09-23 the apply
result came through one, and a worker on another machine could not read it
(P14, four yes votes). The test-window reference owns the pause procedure
itself, this rule owns what the window says to the team.

## Rule 11. The operator is asked on the board, and on Telegram when it is urgent

When you need the operator's input (a choice, an approval, a fact only they
have), send it as mail to `operator` in the project conversation:

- the question
- the options, each with what it costs
- your recommendation
- what you do while you wait

When it is urgent, send it as `question`, `decision` or `blocked`. These kinds
also reach their Telegram, and their reply arrives as a correlated message you
answer like any other.

A Claude Code worker session runs without an interactive prompt tool. It is
started with `--disallowedTools AskUserQuestion` (first-run reference), so the
board mail is the one way a question reaches the operator.

Why: the operator is not watching every worker's terminal. A dialog that
waits in one session holds that worker still, and the operator learns about it only if
they happen to look at that screen. On 2026-09-23 a coordinator asked the operator an
approval question in a terminal dialog. The operator's ruling: "in PB the agents cannot
be sure the operator is looking into their terminals. and if there are inputs
needed, the agent must send this in project chat to operator, or if urgent
then also in telegram."

## Rule 12. Cards are edited only in Connection Hub

An application links to a Card in Connection Hub, optionally with a
preselection or a focus for the purpose the link serves. It never builds its
own Card editor, and it never keeps a stored copy of the decision that it
writes back over the Card. Connection Hub holds the decision. Grouping of
operations (for Problem Board: Review, Work, Plan, People) is declared with
the operations in the service catalog, so Connection Hub groups them for every
client. Why: a second editor in the board kept its own copy and overwrote
edits made in Connection Hub, and every client would have had to build its
own grouping. The operator's ruling (2026-09-26): "it must be cards in the
connection hub ... please do not build new interface for this, this is simply
wrong! and does not scale."

## Rule 13. A behaviour change carries its documentation in the same change

Every change to Problem Board behaviour updates the public documentation
(`repo:app-ecosystem/products/project-board/docs/`) in the same change; when
the code lives in a private repository, the documentation change is a paired
change request in app-ecosystem, named in the first one and merged with it.
The reviewer checks it and refuses a behaviour change without it. Why: the
concepts an agent needs to explain Problem Board had been kept only in private
pages, so an agent with only the public client could not answer who edits
which Card (operator, 2026-09-26).

## From this moment: round 2 on the shared host, 2026-09-22 22:20Z

Round 1 ran four hours under rules that did not exist when it started, and
changed four of them from eight findings. Round 2 runs under the rules as
they stand now. What every agent on the shared host does:

1. **Work from your own workspace.** `~/.kdcube/pb/workspaces/<alias>/<repo>`
   (a host set up before 2026-09-25 keeps its old root until it moves), a
   worktree of the shared checkout (Rule 1 has the commands). Make yours
   before your next edit, nobody makes another agent's. A second item in
   flight gets a sibling tree, removed with its branch when the change
   request merges.
2. **Nobody edits the shared checkouts under `~/src`.** Not to develop, not
   to land. A merge lands on the integration ref, and the coordinator
   fast-forwards the shared checkouts to it before any runtime action.
3. **Every item on a branch, every branch pushed, every review on a change
   request** at a named immutable head, with the link on the item and in the
   report. Delete your branch when it merges or you abandon it.
4. **Intent before the first edit**, `source_in_flight` with the item key and
   the paths, read first, cleared when the change request opens. Overlap
   goes to the coordinator, on the item, both sides named.
5. **Before you ask for a review**, the author's side of the gate: base check
   (`git merge-base --is-ancestor origin/main <head>`), suites on that head
   with counts, a regression shown to fail without the fix, every enforcing
   place listed, docs in the same change request, nothing non-public.
6. **Approval is a board mail naming the head, quoted on the change request
   pinned to it.** The reviewer proves the path the finding is about, over a
   fake that models the constraints the proof depends on, or live.
7. **The coordinator merges, pushes the integration ref, names the merged ref on
   the item, installs procedure revisions, and runs reloads and relay
   restarts** after collecting ready from every agent on the host. Urgency from
   the operator is not an exception: the announcement says so and runs
   anyway, which is still an announcement. The cost of a silent action
   lands on the agents who learn of it from their own broken channel
   (round 2, finding six). After merging a journal change request it
   fast-forwards the shared checkouts, since the index reads them (finding
   nine).
8. **Acceptance is the behaviour observed live**, not the suites on the
   branch: the coordinator verifies the way the operator would, after the
   runtime action that makes the merge live. It is checked line by line
   against the item's acceptance text, and a merge is evidence for the lines
   it touches, never for the item (finding sixteen). Before accepting, the
   coordinator fetches and runs `git merge-base --is-ancestor <commit>
   origin/main` on its own clone for the commit the report names: the report
   is a claim and the clone is the evidence (finding eighteen).
9. **Keep your visible state true**, and **every collision, block, stale
   read or rule that did not fit gets a Round 2 entry** in the log below,
   written by whoever saw it, as it happened, in the project's journal, or as
   a note on W262.

## Interim, 2026-09-22 22:20Z: what is true now versus the target

| target | shared host now | second host now |
| --- | --- | --- |
| one working tree per agent | each agent in its workspace; an agent still in a shared checkout makes its own | one workspace per agent, own clones |
| branches and change requests | every item since 20:10Z, twelve change requests merged or open | from the first task |
| pb runs from a pinned copy | selected package version or composite release, reported by `pb source status`; the shared checkout is not a runtime | selected package version or composite release, reported by `pb source status` |
| coordinator merges and pushes | the coordinator, on the host | the coordinator, remote |
| review record | board mail plus a pinned change request comment, one GitHub account for all | same |

Nobody lands by hand any more on the shared host. On a machine with neither a
coordinator nor an elected integrator, landing an approved change into a
shared checkout goes like this, and the skill points here: take a bounded
turn for a shared Git operation, announced on the dashboard, copy to
`.landing` names in one pass, move in one pass, verify with `cmp`, run both
suites live. Prepare and verify in a private index (`GIT_INDEX_FILE`), commit
by explicit path, then refresh the shared index with
`env -u GIT_INDEX_FILE git read-tree HEAD`, or the next ordinary commit by
anyone records a reversal. Never `git add -A`, never `git stash`.

## Rehearsal log

One entry per round. Record what collided, who was blocked, what nobody could
see, and what changed in this procedure because of it. Observations, not
intentions. The entries go in the project's journal, one per finding, not in
this page: they name the project's hosts, agents and change requests, which a
public procedure never does (Rule 9). The rules above carry what each finding
changed.
