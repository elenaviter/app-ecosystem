Part of [coordinator](../coordinator.md).

## Reload, refresh, restart

Every activation is addressed to a commit: a runtime action releases the
commit its ref names, and a client switch is `pb source use-code --expect`.
The list below decides which commit that is and proves it is the one that
loaded. The commands for a runtime's actions, and what their receipts say,
are in the runtime's profile (`pb worker context`, `runtimes[].local_profile`);
this list is the same for every runtime. Nothing is ever loaded from a working
tree, which stages whatever it holds at that instant.

1. **Integrate onto the named ref first.** The action releases, in each
   repository it loads, a ref the project names for that runtime
   (`pb worker context`, `runtimes[].actions[].releases`), never a working tree. Before anything else, bring the commits that are to
   go live onto that ref, reviewed, and push it where the runtime's machine can
   fetch it; then fetch it on that machine and note the commit it names. On
   one machine this is the same step: the integrator's checkout is not the
   ref until it is pushed and fetched. On several machines, workers push their
   branches, the named merger integrates them onto the ref, and each runtime's
   machine fetches that ref, so no machine loads another machine's working
   tree. The steps below decide nothing a working tree holds: they check that
   the commit the ref names is the one to release, and prove it loaded.
2. **Read the dashboard first**, map every row to its concrete worktree or
   runtime boundary, and act on that relationship. The row says what a worker
   is about to change and `git status` says what has changed, and neither makes a
   hold by itself. A commit-addressed action stages the approved commit from
   its clean release tree. A `source_in_flight` row for an isolated worker
   worktree is therefore informational: that worktree cannot
   change the candidate, so record its owner in the announcement and proceed.
   The row holds the action only when at least one of these is true:

   - the worker can write the same filesystem tree the action will stage, and
     that tree is dirty or can still change after preflight
   - the approved candidate is meant to include the worker's in-flight commit,
     but that commit has not been integrated onto the released ref
   - the runtime action would interrupt or conflict with the worker's current
     local or runtime operation

   For a real hold, ask its owner for the release condition, record it, and
   wait for that condition. A `reload` row requests activation of its named
   commit. Apply the same three conditions to it and proceed when none applies.
   A worker answers the announcement with the same three conditions
   ([test-window](../test-window.md)). Why: every worker develops in its own
   worktree, so only a shared tree, a missing commit or a running operation can
   change what a runtime-only action loads. A host client switch or relay
   restart additionally follows [Host Client Window Quiescence](../runtime-actions.md#host-client-window-quiescence):
   drain and hold future calls before execution, not only those visible now.
3. **Announce** the action, the tree, the approved commit per tree (full
   sha: that commit, not the tree, is what the action loads), and what it
   releases (a worker may
   have published the activation it asks for as a `reload` row with
   targets `bundle:<id>` and `procedure:<package>@<revision>`, so the
   others see a reload coming, and which revision, before the mail): the commits since
   the last activation of that tree (`git log <last>..HEAD -- <tree>`), one
   line each, naming any that are unreviewed. Collect one explicit `ready`
   or `hold` from every attending worker the action affects and that is
   available now; silence is not `ready`. A worker that is unavailable is
   listed as pending with its reason: you establish from evidence whether one
   of the three conditions above applies to it, and it holds the action only
   through one of them. Its absence neither holds the action nor shows that
   none applies. One `hold` stops it. A `ready` that carries a
   constraint (a commit it must be at or after, a window it needs, a file it
   is about to touch) is honoured or the action is re-announced. A `hold`
   names what releases it (a commit, a clear, a time) and the holder sends
   the release. A hold without a condition is asked for one, and when its
   condition is met and verified (the named commit on `HEAD`, the row's paths
   clean) the action may run with the hold quoted. A worker that has not
   answered is asked once more with the deadline only when it is available
   now; a paused, limited or unreachable worker is not woken
   ([Check a silent worker](check-a-silent-worker-do-not-wait-for-it.md)) and is
   handled as pending above. Running without its answer
   is allowed when the exact release tree is clean at the approved
   commit and the worker's dashboard row maps to another isolated worktree or
   otherwise meets none of the three hold conditions above. The announcement
   records the missing answer, the mapped worktree or runtime boundary, and
   that reason. This missing-answer waiver never applies to a host client window:
   use the owning quiescence gate, its bounded acknowledgement deadline and
   non-quiesced record. Every required START send must return OK; a refusal
   stops before execution. Transport acceptance never replaces session handling.
4. **Immediately before**: `git status --porcelain` on the tree and a
   `pb worker receive`. Both are evidence about that moment and neither is the
   guarantee: the tree can change between the check and the staging, and the
   guarantee is an activation addressed to a commit. Run the checks the
   runtime's profile adds for this moment (a descriptor behind the change,
   for example).
5. **Execute** the action as the runtime's profile gives it, at the commit
   the ref names, in the order the profile gives when one window moves more
   than one tree; for the host's client, `pb source use-code` with `--expect`.
   Then **check
   the receipt against the approved candidate**: the profile says which line
   of the receipt names what loaded, and the relay's first stamped line
   (`source=snapshot`, `app_ecosystem=<sha>`) names the client. A receipt that names another commit is a failed
   activation: report it as failed, with both commits, and stop there. An
   action that returns before its build finishes has not said whether the
   artifact is current; only step 6 does.
6. **Verify the deployed artifact, never the commit.** Ask the running
   process for one symbol or behaviour the change introduced, with the checks
   the runtime's profile gives. Relay: the first stamped line of the new pid
   (`file_descriptor_limit=`, `source=`). Package: `pb procedure verify` on
   the host. For a relay, identity is not enough: once the longest designed
   interval has passed after activation (the 900 s workspace walk today),
   compare the relay log with the same length of log before it. Count each
   periodic job's lines against its designed interval, and the slow channel
   turns. A periodic job that runs more often than designed, or a slow-turn
   rate several times the earlier window's, is a failed activation: report
   both counts, hold the ALL CLEAR and keep the rollback. Why: on 2026-10-02 an
   activation passed identity proof while its workspace walk ran on every
   heartbeat (278 walks in 48 minutes for 3 channels against one per 900 s
   each), readable from the release's own log lines that nobody compared.
7. **Say what loaded**: the pid or the profile's receipt, the ref and the
   commit it named, the commit range, and, for
   each worker whose commits rode along, that they did. Clear your dashboard
   row. A worker asking "what did that release" is asking for this line.
8. **Bump the app's release record.** A board release updates the app's
   `release.yaml` in the same change as the release: its version and a dated
   note naming what the release carries. A release record left behind tells
   nobody what runs.

**New operations reach an agent only through the project Control Card.** A
project's Control Card caps every agent Card in the project (AND), and a Card
Refresh is capped by it too. After new operations reach the catalog, a project
admin ticks them on the project Control Card in Connection Hub first, and
only then refreshes Cards. A refusal that names the Control Card
(`work_worker_operation_withheld_by_control_card`) means exactly that step is
missing: re-consent and re-publishing the catalog do not help. Why: on
2026-09-26 a coordinator refresh after new operations reached the catalog
changed nothing until they were ticked on the project Control Card.

A LaunchAgent or systemd unit is host service configuration: `pb relay-service
install` is typed by an agent after the operator approves it, so it is theirs
to clear, as is any action the runtime's profile says rebuilds their stack.
Restarting a service whose definition exists is a coordinated runtime action
and needs no more than the list above.

`pb source use-release` and `pb source use-code` change the immutable source
used by both the host command and that service, and include the restart needed
for the relay to observe it. Announce the exact package version or, for code,
the full App Ecosystem commit. Collect the same host-local
readiness, and verify `pb source status` plus the new relay startup record
before reporting the move complete. `pb procedure install` installs the
procedure package of the release `pb` runs, so a procedure change merged on
`main` reaches a host only after its source moves to a commit that contains
it: switch the source, then install for each target, then `pb procedure
verify` (2026-10-02, W455).

When a host still runs the checkout client, follow the cutover section in
[runtime actions](../runtime-actions.md) before fast-forwarding that checkout.
The four-package source install, commit verification, and relay restart are one
precondition for the fast-forward, not recovery steps after it.
