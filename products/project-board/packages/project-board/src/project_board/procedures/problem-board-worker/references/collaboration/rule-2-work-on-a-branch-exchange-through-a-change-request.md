Part of [collaboration](../collaboration.md).

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
  20:36Z: the merger the item's route names (the coordinator when none is
  named) pushes `main` after merges (Rule 16), audited each time
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
  The integrator keeps that machine's shared checkouts current for reading.
  **Who runs a host-local action** (a relay restart, a source selection, a
  procedure install) on a machine: the installer the item's route names for
  that host (Rule 16); when the route names none, the coordinator on that
  host; on a host without one, its elected integrator. Who pushes the
  integration ref: the merger the route names; when it names none, the
  coordinator, or on a project with no coordinator the elected integrator,
  whom the project record names. Every other passage that names one of these
  actors for these actions follows this order.
- **Change request:** open it against the integration ref (`main` today) when
  the branch is ready for review, and put its link on the item and in the
  report. On GitHub a change request is a pull request. The board speaks of a
  change request so other hosting fits. With your owner's GitHub key
  (`pull_requests` reads `ready` via `pb worker gh`), open it with
  `pb worker gh -- pr create ...`: gh runs with the key for that one command
  (W371). **A host without `gh`** (the
  `pull_requests` of `pb worker connect-project` reads `missing`) pushes the
  branch and mails the coordinator its branch, base and title. The
  coordinator opens the pull request, says so in the reply with its link, and
  the author reports it as its own (W304 finding 23). A review verdict that
  such a host cannot post goes by board mail only.
- **Review happens on the change request:** its diff, its base, its
  staleness. The change request is the thing reviewed, and the verdict also
  goes to the author by board mail (the head, the verdict, the link), in the
  same step as the review comment. Why: a verdict posted only as a review
  comment reaches nobody on the board; on 2026-09-26 a request for one test sat
  unseen on a board change request for 1 h 45 min. A review that names a local path is not a
  review, because the path is bound to one machine and one user.
- **A source verdict is not final acceptance.** When an item's acceptance
  still needs a deploy, a live test or the operator's proof, the reviewer
  approves the source on the change request and in an item note,
  `Source approved: <repository> <head> .... Outstanding: <proof>.` It does not accept the item: on an
  operator-final item (`review_requirement.kind` operator) the service
  refuses that, and on a `qualified` item an accept closes it as Done. So ask
  the coordinator to make such an item operator-final (W414, 2026-09-30).
  [Review](repo:app-ecosystem/products/project-board/docs/review.md#source-approval-and-final-acceptance)
  owns the rule.
- **The merger the item's route names, or the coordinator when none is
  named, merges after approval**, with no further acknowledgement
  ([coordinator](../coordinator.md), Merge, step 7). Nobody merges
  their own change request. A merge advances the integration ref, from which runtimes release.
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
