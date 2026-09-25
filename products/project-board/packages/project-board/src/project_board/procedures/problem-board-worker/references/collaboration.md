---
id: applications.playground.problem-board.skill-reference.collaboration
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
version and `pb source use-code` selects exact App Ecosystem and KDCube commits
as one release for both command and relay. A direct checkout invocation is a
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
- **Say your scope in the `working` report, and declare your worktree once.**
  Team decision 2026-09-23 (P4b, P4a). The first `working` report carries one
  optional line, `--scope`, naming the module, path prefixes or runtime
  surface you will change, set again only when the boundary grows. Then
  declare where on this host you edit each repository the assignment binds,
  once, so the relay publishes the tracked files you have in flight:

  ```bash
  pb worker report --state working --scope 'client/relay.py, services/control.py heartbeat' ...
  pb worker workspace --assignment-ref <assignment-ref> --repository repo:app-ecosystem/products --path ~/workspaces/me/ae@w278b
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

When you need the operator's input (a choice, an approval, a fact only she
has), send it as mail to `operator` in the project conversation:

- the question
- the options, each with what it costs
- your recommendation
- what you do while you wait

When it is urgent, send it as `question`, `decision` or `blocked`. These kinds
also reach her Telegram, and her reply arrives as a correlated message you
answer like any other.

A Claude Code worker session runs without an interactive prompt tool. It is
started with `--disallowedTools AskUserQuestion` (first-run reference), so the
board mail is the one way a question reaches her.

Why: the operator is not watching every worker's terminal. A dialog that
waits in one session holds that worker still, and she learns about it only if
she happens to look at that screen. On 2026-09-23 a coordinator asked her an
approval question in a terminal dialog. Her ruling: "in PB the agents cannot
be sure the operator is looking into their terminals. and if there are inputs
needed, the agent must send this in project chat to operator, or if urgent
then also in telegram."

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
   written by whoever saw it, as it happened. Mail fable-pub or note it on
   W262.

## Interim, 2026-09-22 22:20Z: what is true now versus the target

| target | dev-main now | spark1 now |
| --- | --- | --- |
| one working tree per agent | fable-pub in its workspace, three agents still in the shared checkouts until they make theirs | one workspace per agent, own clones |
| branches and change requests | every item since 20:10Z, twelve change requests merged or open | from the first task |
| pb runs from a pinned copy | selected package version or composite release, reported by `pb source status`; the shared checkout is not a runtime | selected package version or composite release, reported by `pb source status` |
| coordinator merges and pushes | claude-main, on the host | claude-main, remote |
| review record | board mail plus a pinned change request comment, one GitHub account for all | same |

Nobody lands by hand any more on dev-main. On a machine with neither a
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
- **23:52Z, finding six: the coordinator ran a stack rebuild without
  announcing it or collecting ready** (recorded by claude-main, in its words).
  At 23:50Z it merged KDCube #261 and ran `kdcube refresh --build` straight
  after, because the operator was waiting to retest the picker. Three workers
  were mid-flight, two lost their channels for about five minutes, and one
  spent that time diagnosing whether the platform was failing. Ready had been
  collected for the two earlier windows that night, not for this one, because
  it felt like "just a rebuild" while someone was waiting. The rule it
  confirms: a runtime action is announced and ready is collected before it
  runs, and urgency from the operator is not an exception. If the operator
  needs it immediately, the announcement says so and runs anyway.
- **23:57Z, finding seven: a channel's failure record outlives its reopen.**
  During the rebuild the relay's first opens failed with
  `oauth_challenge_not_advertised` and it recorded the failure in
  `relay-pacing.json`. It reopened fable-pub's channel at 23:57:25Z, but
  `pb status` and `pb coordinate` kept refusing with
  `work_coordinate_channel_reconnecting` until 00:01:21Z, because the record
  is removed only by `record_success` after a completed cycle poll, and the
  first cycle after the restart completed at 00:00:32Z. Three minutes seven
  seconds of an open channel refused on a stale record, in the `pb` process
  before any request reaches the relay's side server (W267 cannot help
  there). A code defect, filed as W274 and fixed the same night (change
  request #17, merged 48035dcf), landing identically in the app's relay and
  the W255 package copy. The procedure point it confirms: two views of one
  channel must not disagree, and the refusing view must be the live one.
- **00:20Z, finding eight: who reviewed a change request must be visible
  without asking** (recorded by claude-main). Twice in one hour a review
  request reached a second reviewer after the coordinator had reviewed and
  merged the change request itself (KDCube #260 and #263). Nothing was
  wrong: gate 1 was met by the coordinator as the non-author. The second
  read still found two paragraphs the first had not, so it earned its
  place, but the turn spent finding out the merge had happened was
  avoidable. Rule 5 gate 1 gains the merger's duty: when the merger is
  also the reviewer, it tells every other invited reviewer at the moment of
  merging. The reviewer of record lives only in the verdict mail and a
  hand-written comment today. A place on the item is the target.
- **01:05Z, finding nine: a merged journal entry cannot be indexed until the
  shared checkout is fast-forwarded.** `pb worker journal-index` resolves a
  `repo:` reference against the shared checkout, by design: the journal home
  is the repository binding, and a worker's own tree can be ahead, behind or
  on a branch nobody merged. After #18 merged, the index refused with
  `journal_entry_not_found` until claude-main fast-forwarded the checkout.
  Round 2 point 7 gains the step: after merging a journal change request the
  coordinator fast-forwards the shared checkouts before the author indexes.
- **01:35Z, finding ten: the journal stamp rule was right, unenforced, and
  decayed within a day.** The journal directory is read in filename order and
  the filename stamp is the address other records cite. On 2026-09-12 every
  entry's filename stamp `YYYY.MM.DD.HHMMSS` equalled `recorded_at` in UTC,
  about thirty entries. From 2026-09-13 the stamps were local time, two hours
  ahead, in about 110 entries by every worker over ten days, and nothing
  noticed until a UTC entry written on 2026-09-23 sorted above entries written
  after it (change request #18). The census that followed found a drift, not
  a convention, and found the `entry_ref` stamp UTC throughout, with two
  exceptions from 2026-09-15 that are named in the test and closed. Ruling by
  claude-main at 01:36Z: UTC stays the rule (hosts in different zones, a DST
  change, filename order), enforced from 2026-09-23T00:00:00Z by
  `tests/test_journal_filename_stamp.py` (change request #22) for the filename
  stamp and for every entry's `entry_ref` stamp, with the rule and its reason
  in the failure message. Merged filenames before the cutoff are addresses and
  keep their names, a correction goes inside the entry. The feature journals
  under `kdcube-docs/journal` (1394 entries, a four-digit local stamp and a
  zoned `Date:` line, no `recorded_at`) are another convention and out of
  scope until the operator says otherwise. The lesson under it: before
  enforcing a rule read off a handful of neighbours, census the directory.
  The four corrections written into merged entries that night answered a
  rule nobody had checked was the practice.
- **01:55Z, finding eleven: a budget met by compression traded facts for
  lines** (recorded from claude-main's review of #21). The skill absorbed
  W276 three times by tightening prose to hold 519 newlines, and the third
  time it cost content: `--format brief` "anywhere on the line", "for a
  question or request the correlated `send`", `pb render` rendering saved
  output "the same way", the collaboration procedure's name and "one
  rehearsal round at a time" in the sentence that introduces it, "on a
  machine where the shared checkout is also the live `pb` runtime an edit
  there is live for every worker at once", and "GitHub sees one account for
  all agents" as the reason approval lives in mail. Meanwhile the new
  journal-stamp sentence ran unwrapped past every other line's width, so the
  budget was met by old prose while new prose was not held to the shape.
  Every fact is restored, the receipt, error and ref detail moved to
  `references/brief-output.md` behind one trigger line, the contract test
  pins the restored facts, and gate 8 names the rule. The budget itself was
  never undeclared: the test carried it with its reason since 2026-09-18.
  What was missing was the rule that follows from it, written where a
  reviser reads.
- **02:06Z, finding twelve: a reported count was a claim about evidence,
  and the two were indistinguishable to the merger.** Fixing finding eleven,
  the author's command chain ran the contract suite, filtered its output
  through `grep | tail`, and took that pipeline's exit status as the gate.
  pytest had failed on `never composed from a key and a title`, the very
  class gate 8 is about, a fact leaving under compression. The chain
  committed, pushed 4a8f487b and mailed "contract 19 passed, suite 1221
  passed" to the reviewer and the merger, who was about to merge on it. The
  author caught it in the same output a minute later and said so before
  anyone merged. Two halves. The author's: gate 3 now says the counts are
  what the tool said, pytest's summary line verbatim from the run at that
  head, with pytest's exit status checked and never a pipeline's. The
  merger's, taken by claude-main: a reported count is not verification, so
  the merger runs the suites on the exact head before every merge in this
  repository. The rule under both: the thing that checks must be the thing
  that reports.
- **02:20Z, finding thirteen: two kinds of evidence were treated as one,
  and the reading won over the measurement.** Closing W255, fable-pub read
  project-board's direct dependencies and wrote that the worker host is one
  repository. codex-ui installed the package into a clean environment at the
  named commit and got `kdcube-cli` unresolvable from `connection-hub-cli`,
  which declares it and imports it in twelve modules. claude-main propagated
  the reading over the measurement, and codex-ui dropped working
  composite-selector code on it. The rule, in gate 4: a claim about what a
  host installs, or what a closure contains, is settled by installing it
  into a fresh environment at the named commit. Reading a `pyproject` forms
  the claim, it does not settle it, because the environment you already
  have is the one that hides the answer.
- **02:57Z, finding fourteen: a truncated listing hid a changed file from an
  approval.** Approving app-ecosystem #12 at bf25ed1, fable-pub's delta
  listing was cut to four of five files by a `tail`, and the approval covered
  a file nobody had read. The reviewer noticed after approving and said so on
  the change request and by mail. Gate 1 now carries what the skill already
  says for mail: two counts, the files the change request lists and the
  files the reviewer read, both stated in the approval, and truncation never
  means the omitted do not exist.
- **03:40Z, note: `source_mismatch` is exercised only through the fake.**
  W255's `await_source` classifies a startup record naming another release
  as `source_mismatch` and rolls back, and no test feeds it a real startup
  record with a different `release_id` after the restart. One test to add in
  the package, not a condition on the heads.
- **03:43Z, incident: a bare rebuild took the published connection-hub.**
  `kdcube refresh --build` without `--maintainer-local-python-package` for
  every first-party package installed the published connection-hub, which
  has no `server_side_login`. chat-ingress and chat-processor died, the web
  proxy answered 502, and every relay channel reported
  `oauth_challenge_not_advertised` for thirteen minutes while four agents
  read a gateway error as a missing OAuth challenge. Keepers: the rebuild
  command is the documented maintainer one (the maintainer rebuild procedure, `repo:app-ecosystem/products/kdcube/procedures/maintainer-rebuild.md`), and
  exit 0 plus "every container started" is not verification, verify against
  the containers and an unauthenticated probe of the endpoint. connection-hub
  discovery reports any non-401 as a missing challenge and has to report a
  gateway error as one (codex-ui, after). The shared applications checkout
  was dirty at the time (`source.dirty true` at f74c3a99): someone had edited
  the live runtime tree.
- **03:56Z, note: the wake path is not the channel.** `pb worker inspect`
  reported `wake delivery failed` while the channel was active. A reader who
  sees it during a channel problem will take the two for one thing, as four
  readers took a 502 for a missing challenge. Understand the wake path's own
  failure before it becomes its own incident. Also: `relay.stderr.log` on
  dev-main was 800 MB and unrotated.
- **04:03Z, finding fifteen: a gate that names no action is a report.**
  `PROBLEM_BOARD_HOST_PYTHON` was set by nobody but a reader of one
  paragraph, so two installed-host checks in W255 were documentation. The
  check belongs at the moment the interpreter exists: the add-a-worker-host
  procedure ends its install step by running `testing.md`'s post-install
  suite with the variable at the venv the installer wrote, and the
  coordinator records pytest's line on each cutover. Under Rule 5's intro
  since this revision: a new gate names what a reader does to satisfy it,
  with a pointer to the means, or it is not a gate yet. A verification that
  ends by running the suite it just made runnable is a verification. One
  that stops at printing JSON is a report. Gate 3's merger clause and
  finding ten's stamp rule were both written without the thing that makes
  them followable, and only the rule got reviewed.
- **05:20Z, note: three at-most-once answers, consistent once the unit was
  named.** W276: the unit is the write, repeating is safe under the same key.
  W257: the unit is the issuance of a bearer, the store enforces
  at-most-once. W247: the unit is the acknowledged delivery, not the fetch,
  so a device-bound ciphertext may be fetched again until the device
  acknowledges, every fetch recorded and shown on the Card. The axis that
  predicts the answer rather than describing it: whether the repeated thing
  can be used by someone else (a bearer can, a JWE bound to the fetcher's
  key cannot, an idempotent write yields the same receipt). A decision of
  this kind names the residual it accepts (W247: a device key compromised
  before delivery, identical under either rule, detected at different
  moments) as well as what it protects.
- **06:05Z, finding sixteen: an item was accepted on a merge that covered
  one of its five acceptance lines.** W253 was accepted on "merged through
  the full gate" when the merge covered acceptance[5] only. Lines three and
  four were unmet and the code said so in a test name,
  `test_oauth_grant_store_stays_on_redis_before_migration_cutover`. codex-ui
  found it reading the artifact, claude-main reopened the item. Round 2
  point 8 now says acceptance is checked line by line against the item's
  acceptance text, and a merge is evidence for the lines it touches, never
  for the item.
- **06:26Z, finding seventeen: a suite that skips what the change touches
  is green about everything except the thing under review.** Merging
  app-ecosystem #13, claude-main's count at 0518f213 was 663 passed, 21
  skipped, against the reviewer's 667 passed, 17 skipped from a fresh
  environment. The four were the PostgreSQL authority tests, the exact
  tests covering the change, skipping on `CONNECTION_HUB_TEST_POSTGRES_DSN`
  unset. claude-main ran them against a throwaway database, 14 passed, and
  dropped it. The fix is a habit rather than a tool: the reviewer compares
  their count with the author's and asks about the difference, and the
  approval states its inputs so the two counts are comparable at all (gates
  1 and 3). The same night the dev-main chat-processor venv lacked
  `jwcrypto`, declared by connection-hub, so sixteen modules failed to
  collect with errors that looked nothing like the cause: `testing.md` named
  an interpreter and six overlays and checked nothing about what the
  overlays declare. It now carries a dependency preflight, and its first run
  named two more declared dependencies that venv lacked, `readchar` and
  `json5`, one import away from the same failure. A check imports what the
  code imports: the first check of the `jwcrypto` install asked for
  `jwcrypto.__version__`, which that package does not define, and would have
  reported a good install as broken.
- **06:29Z, finding eighteen: a journal entry saying work landed is not
  evidence that it landed.** Three reviewed W253 commits (25226d04f,
  10eac3d4a, f8f081954), journaled on 2026-09-22 as landed, were in no
  branch and absent from `origin/main`, held only by the reflog, one `git
  gc` from gone. codex-ui's audit found it, claude-main verified it,
  preserved them as local tags on dev-main and told the operator, and
  fable-pub confirmed from a third clone that the installed procedure asked
  only the base-current direction of `is-ancestor` and nothing about where a
  completed report's commit has to be. The same failure two days earlier:
  claude-main accepted W253 on a report naming add1940 without running one
  command on its own clone. Two sides and a journal clause, in Rule 6 and
  round 2 point 8: a completed report names a commit and the integration
  ref that contains it, the reporter having fetched and run `git merge-base
  --is-ancestor <commit> origin/main`, the acceptor running it on their own
  clone before accepting, and a journal entry that says landed names the
  merge commit on the integration ref and is written after that commit is
  fetched, never from the intention to merge.
- **12:33Z, finding nineteen: a push announced after the merge.** fable-pub
  told the coordinator it was pushing an amendment to applications #29 while
  #29 had merged at 12:32:44Z. No push happened, so nothing was lost, and
  the announcement was still wrong about the world: a merged change request
  takes no more commits, and a branch pushed after its merge is a new change
  request nobody asked for. Rule 5, the author's side: read the change
  request's state immediately before every push to it, and after a merge put
  the amendment on a new branch and open a new change request. The author
  opens and reopens change requests, the coordinator never opens one on the
  author's behalf.
- **14:15Z, finding twenty: JSON envelopes in the model's context.** The
  operator noticed codex-main compacting every few turns and reading messages
  without their form. codex-main's own words: "I also amplified it by letting
  settlement commands print full JSON bodies instead of --format brief; that
  was my tooling mistake. I'm switching every remaining PB command to bounded
  brief output." The skill had said `--format brief` and the session default
  since 2026.09.22, as a description of what pb accepts. It now says it as a
  rule with its reason, and names `export PB_FORMAT=brief` once per session
  as the way that cannot be forgotten per command (revision 2026.09.23.3,
  [brief-output](brief-output.md)). The operator's instruction: "lets make
  sure this is taken on our skill/procedure." The class fix, pb rendering
  brief by default when its output is not a terminal, is a product decision
  raised with the operator.
