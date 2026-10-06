Part of [coordinator](../coordinator.md).

## Accept, return, cancel

1. The submission is read against the item's acceptance lines, one by one, and
   against the deployed artifact where a line is about behaviour (see Reload
   below for what "deployed" means per tree).
2. A pull request to the board's server app repository is accepted after the app's
   Python suite runs on `main` with the change merged, widget-only changes
   included: the contract tests read widget source. A failure is compared
   against the same suite on `main` before the change.
3. A submission that closes a line with "that case cannot occur" is returned,
   not accepted, until it names the observation that would show the case
   occurring. A claim is derived from the observation that would falsify it
   and carries that observation's timestamp. A name, a timer or a threshold is
   not one.
4. An item whose acceptance needs a deploy, a live test or the operator's
   proof is operator-final: set `review_requirement` to `{"kind": "operator"}`
   when you create or route it (`plan.item.update`). Its source reviewer
   records source approval as a change-request verdict and an item note
   (`Source approved: <repository> <head> .... Outstanding: <proof>.`), not
   as an accept, which the service refuses to an agent; a source defect is
   returned with `review.return` as usual. After the
   verified deploy, route the Review to the person with `review.assign`
   (`operator` or `operator:<user id>`, with the merged commits and the
   deploy check) and a `decision` mail; their accept is final. Never accept
   such an item as done on source alone (W414, 2026-09-30). [Review](repo:app-ecosystem/products/project-board/docs/review.md#source-approval-and-final-acceptance)
   owns the rule.

   Name a change's delivery state with one of these words, each with its own
   evidence. No state implies the next (W449):

   | State | Evidence |
   | --- | --- |
   | source-approved | a review verdict at the exact head |
   | integrated | the head is on `main` and the merged tree is proven (Merge, step 3) |
   | installed | per host and provider, the `pb source status` and `pb procedure verify` receipts |
   | live | the activation receipt and its verification |
   | operator-accepted | the operator's decision on the item |
5. `review.accept`, `review.return` and `review.cancel` take the item
   `work_ref` looked up from `project.plan.item`, its `expected_revision`, and
   an `idempotency_key` you generate for this decision. Return and cancel take
   a reason. The service refuses the worker that submitted the work from
   deciding on it (`work_review_self_forbidden`), for all three decisions.
6. The item is the record. After the decision, read the item back: status
   `done` for accept, `working` with the same assignee for `review.return`
   (the worker keeps the assignment, its ownership version advances, and it
   reworks against the new version), `cancelled` for cancel, and the
   assignment state beside it. Selecting Todo for an item in Review is a
   different edit: it records a return that leaves the item in Todo, as
   [Review](repo:app-ecosystem/products/project-board/docs/review.md) says. To hand returned work to someone else, change its assignee: that
   one edit notifies the new assignee and makes the item theirs, in any status
   and with the status left as it is. Assignee and status are independent
   edits in either order, and neither needs a review command first (operator,
   2026-09-29, delivered by W398). Mail about the decision is commentary.

7. **Route the reviews an author could not place (W326), in the turn they
   arrive.** The author arranges its own review with available qualified
   teammates and replaces a reviewer who becomes unavailable
   ([collaboration](../collaboration.md) Rules 6 and 16); you are not asked to
   approve that choice. An item comes to you for review only when its author
   found no qualified available reviewer or the review stayed blocked, with a
   review request in your inbox. Handle it in the turn it arrives and decide
   who reviews:
   - **yourself**, when you can check everything the item asks, with
     `review.assign` naming yourself, so that you are the item's assignee;
   - **another agent** linked to the project, other than the one who did the
     work, with `review.assign` (`pb coordinate review.assign --object-ref
     <project> --payload-json '{"work_ref": "<item ref>", "reviewer":
     "<stable worker name>"}'`);
   - **the operator**, whenever `review.look_at` or `review.could_not_verify`
     needs a person's browser or account: `review.assign` with `operator` and
     `"integration": {"merged": [...], "deploy": "<window>: <check>"}` (or
     `"nothing_to_deploy": true`) once it is merged and deployed. The board
     refuses the operator without that evidence and names what is missing.
     `operator` is any person in the project: every person's Review
     Assignments lists it and the first decision clears it for all. Use
     `operator:<user id>` only when a particular person must look (operator
     ruling, 2026-09-26).
     Also send the operator a board mail of kind `decision`, so it reaches
     their Telegram, naming the item, the exact check, and where it now
     appears (Review Assignments). The same holds for anything else that
     waits on the operator, from you or from a worker
     ([collaboration Rule 11](../collaboration/rule-11-the-operator-is-asked-on-the-board-and-on-telegram-w.md)).

   An item in Review whose assignee is still its author is an unrouted review,
   not a state to explain: ask the author for the reviewer it arranged, and
   route it yourself in the same turn when it has none (W446, 2026-10-01: a
   completion without a reviewer stayed on its author for about 70 minutes).

   Never leave a review on the operator by default: the operator's review
   list is exactly the items that name the operator. If you cannot route it
   (for example your Card lacks `review.assign`), tell the operator at once
   with a notifying kind (`blocked`). Never leave the item waiting, and never
   mention it only inside a longer list. Why: on 2026-09-26 W15 waited from
   06:24Z with the coordinator as default reviewer, while its remaining check
   needed the operator's browser, the operator's Review Assignments list was
   empty, and the item showed only under the worker's name.

### Merge

Tested merged trees and review trees live in `<workspace>/rv/`, and each is removed once its merge or verdict is recorded ([project workspace](../project-workspace.md), section 6).

The merge gate is collaboration Rule 5. These steps are the merger's part of
it, in the order of the act.

1. **Read each change request's base before merging it.** Before merging a
   change request whose base is not the integration branch, retarget it to
   `main` (`gh pr edit <number> --base main`), or merge it only after its base
   has merged and it has been retargeted. Never merge a stacked change request
   into its base branch after that base landed: the merge succeeds, the
   change request reads merged, and its content never reaches `main`. Why: on
   2026-09-26 app-ecosystem#203 still had #199's branch as its base when #199
   had already merged, so #203 landed in that branch; only the tree
   comparison of step 3 caught it, and #217 carried the branch into `main`.
2. **A current base, or a tested merged tree.** When approved heads are
   behind `main`, the merger may test the exact merged tree instead of asking
   for a rebase: merge the approved heads onto `main` locally, in the merge
   order, and run each affected repository's suites on that tree (gate 3), stating
   the counts. Record the tested tree (`git rev-parse HEAD^{tree}`). Why: a
   rebase round costs every author a turn, on 2026-09-26 on a quota-limited
   pool, and the merged tree is what gate 2 exists to test.
3. **After merging, prove `main`'s tree equals the tested tree.** Fetch, then
   compare `git rev-parse origin/main^{tree}` with the tree recorded in step
   2 (or, for a change request with a current base, with its tested head's
   tree merged onto the `main` it was tested against). Unequal trees mean
   something landed that was not tested, or something tested did not land:
   stop, find which, and repair before any runtime action releases `main`.
4. **The merger sets the procedure revision; authors never bump it.** A
   change request that edits the worker procedure package arrives without a
   revision change (`package.json`, `procedure-revisions.json`, the revision
   pins in the package's tests). The merger sets the next revision at merge
   time, in merge order, with one commit on the merged branch, pushed before
   the merge, and the tested tree of steps 2 and 3 includes that commit. The
   merger's suite run after that commit sets `PB_REQUIRE_REVISION_RECORDED=1`,
   so the ledger check that skips on an author's head fails if the revision
   is not recorded. Why:
   on 2026-09-26 #207 and #208, then #228 and #229, each claimed the same
   revision (operator, 2026-09-26 20:45Z).
5. **Close what landed.** When a change request's content reaches `main` other
   than through its own merge (an integration branch, a merge train, a stack
   merged as one), prove that its exact head is an ancestor of `main`, or for a
   squash prove an equivalent tree. Then retarget the change requests based on
   it, close it with a comment naming the integrating commit, and keep any
   unique conflict resolution. Reconcile the change-request ledger with the
   plan, review and deployment state at the same time, and declare a real
   parent, base or deploy dependency in that ledger. A head on `main` is not a
   deployment. Why: on 2026-10-01 eight change requests whose content was
   already on `main` were still open (W442, W449).
6. **One release manifest per batch.** When several items ship together, keep
   one manifest on the batch's work item: per original item, its exact
   commits, what it depends on, its review evidence (reviewer, exact head,
   verdict), who deploys it where, and how it is verified live. The items keep
   their own acceptance and progress; the batch deploys once, not once per
   item. Why: on 2026-10-02 the communication fixes of several items had
   merged on `main` and were still not running anywhere until they shipped as
   one batch (W461). "Where" is every host the project runs agents on, read
   from the team in `pb worker context`, not only the hosts of this window: a
   host not running now reads `not running, update on return`, and the ALL
   CLEAR names it so. Why: "we have multiple machines" (operator, 2026-10-02,
   after a window that named two of three hosts).
7. **Integrate promptly after an independent PASS.** When an exact head has
   an independent reviewer's PASS and the gate's checks on that exact source,
   merge it in that turn or the next, or write on the item the concrete
   blocker, its owner and a checkpoint. An unrelated batch, a further
   approval round or a rerun on an unchanged tree is not a blocker: a tree
   equal to the gated tree reuses the gate's evidence, and only what changed
   runs again, also when the merge passes to another person. The item's
   route and the batch's roles table name the merger, a permitted agent whose
   Git access allows it and who did not author the change. That merger
   merges an exact head with an independent PASS and the named gates without
   another acknowledgement from you, and keeps the tree proof of step 3. The revision cut of step 4 may
   be delegated the same way, after the author confirms the head is frozen.
   Cut a revision only for a changed procedure payload, never again for the
   same one. Why: on 2026-10-02 approved heads waited for one coordinator
   thread while reviewers and gates were idle (W466).
