Part of [collaboration](../collaboration.md).

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
   to land. A merge lands on the integration ref, and a runtime action loads
   the exact ref it releases, never a checkout (Rule 2).
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
7. **The merger the item's route names (or the coordinator when none is
   named) merges, pushes the integration ref and names the merged ref on the
   item; the installer the route names installs procedure revisions and runs
   reloads and relay restarts** after collecting an explicit ready from every
   affected agent that is available (Rule 10). Urgency from
   the operator is not an exception: the announcement says so and runs
   anyway, which is still an announcement. The cost of a silent action
   lands on the agents who learn of it from their own broken channel
   (round 2, finding six). After merging a journal change request, the worker
   that indexes it fast-forwards its clean journal clone first, since the index
   reads merged content from that clone rather than the item worktree (finding
   nine).
8. **Acceptance is the behaviour observed live**, not the suites on the
   branch: the verifier the route names (the coordinator when none is named)
   verifies the way the operator would, after the runtime action that makes
   the merge live, and the coordinator accepts from that evidence. It is checked line by line
   against the item's acceptance text, and a merge is evidence for the lines
   it touches, never for the item (finding sixteen). Before accepting, the
   coordinator fetches and runs `git merge-base --is-ancestor <commit>
   origin/main` on its own clone for the commit the report names: the report
   is a claim and the clone is the evidence (finding eighteen).
9. **Keep your visible state true**, and **every collision, block, stale
   read or rule that did not fit gets a Round 2 entry** in the log below,
   written by whoever saw it, as it happened, in the project's journal, or as
   a note on W262.
