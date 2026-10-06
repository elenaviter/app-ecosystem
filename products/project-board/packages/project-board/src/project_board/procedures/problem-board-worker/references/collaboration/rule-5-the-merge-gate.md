Part of [collaboration](../collaboration.md).

## Rule 5. The merge gate

The merger the item's route names (Rule 16), or the coordinator when none
is named, merges a change request when all of these hold, and refuses
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
   A change that puts an `await` where there was none (blocking work moved to
   a thread, an executor or a child process) is reviewed around that await,
   not only in the helper. Every condition checked before it that the code
   after it relies on is checked again after it. Every multi-step write it
   now splits either completes as one step or rolls back when the caller is
   cancelled, and a failure under cancellation still reaches its cleanup:
   `CancelledError` is not an `Exception`, so `except Exception` cleanup does
   not run. Ask for a test that changes the state, or cancels, at that await.
   Enumerate the awaits the change adds (for example
   `git diff <base> <head> | grep '^+.*await '`) and check each one against
   these clauses: a review that does not list them has not applied the rule.
   Why: on 2026-10-02 such awaits in one change stream caused four defects.
   Twice a replaced Card or a closing session still dispatched, once a
   cancel split a stored credential from its profile, and once a failure
   met a cancel and the grant was never revoked. The helper tests passed
   every time, and the fourth passed a review after this rule existed,
   because the review checked the awaits it remembered instead of the list
   (W461, PR 438 and PR 440).
   The counts are what the tool said, not what the shell returned: the
   report carries pytest's own summary line verbatim for each suite, from
   the run at that head, and the author checks pytest's exit status, never
   a pipeline's. Why: on 2026-09-23 a chain read `grep`'s status over a
   failing pytest and pushed 4a8f487b as green (round 2, finding twelve).
   The merger's counterpart: a reported count is a claim about evidence, so
   the merger relies on the independent reviewer's counts at that exact head
   and runs the suites itself for what changed since that run (a new head, a
   moved base, a merged tree, another environment), never again only because
   the merge passed to another person ([coordinator](../coordinator.md), Merge,
   step 7). The command,
   its interpreter and the source overlays it needs are written once, in the
   application's `procedures/testing.md`, section Run The Package Suite. Do
   not rediscover them: a bare interpreter or the relay's fails on imports
   that read like regressions and are not. That section keeps checkout
   variables, not machine paths, so it reads the same on every host: fill
   them from your own machine. A placeholder is not a missing value.
   The second counterpart, for the reviewer and for a merger that ran a
   suite: compare your count with the author's and ask about the difference. A skipped test names its
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
7. **Publication and attribution as the project rules them.** Nothing that
   should not be public in the branch, the commits, the description or the
   comments, for a public repository; and every commit's trailers and the
   description's attribution lines follow the policy the project's Facts
   state for that repository. The named merger checks this on the exact head
   before it merges, because it is the one who lands it, and returns a change
   that breaks it; merged history is never rewritten. The repository-specific
   policy lives only in the project's Facts, never here. Why: on 2026-10-03
   Ops found the trailer check still written as the coordinator's alone
   while named non-author mergers were landing changes (W537).
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

After the merge the merger names the merged ref on the item and hands it to
the next actor the route names; the coordinator tracks it (Rule 16). A runtime
action releases that ref, never a working tree (see
`problem-board-worker/references/runtime-actions.md`).
