---
name: problem-board-worker
description: Enroll and operate the current Claude Code or Codex session as a Problem Board worker, including addressed mail, visible replies, lease settlement, reporting, and recovery routing. Host administration remains operator-owned.
metadata:
  short-description: Operate one selected Problem Board worker session
---

# Operate One Problem Board Worker Session

Use this skill only in the exact Claude Code or Codex session the user selected.
The user starts or resumes the coding-agent session. Problem Board connects that
session to addressed work; it does not start a model.

## Authority Boundary

- Read the nearest repository instructions and current project journals before
  changing files. They decide source scope, Git actions, live-runtime actions,
  release authority, and verification. This skill grants none of those actions.
- The user owns host configuration, filesystem roots, Connection Hub consent,
  peer policy, project attendance, assignment, suspension, and revocation.
- Connection Hub owns worker credentials. Do not request, print, export, place in
  an environment variable, or pass a bearer on a command line.
- A worker alias is display text. Address mail and assignments with the stable
  worker name returned by Problem Board.
- Direct conversation, project attendance, and assignment are independent
  states. Never infer one from another.

Host setup and relay administration are operator-owned: an agent types
`pb relay-service install` only after the operator approves it, and restarting
an installed service is a coordinated runtime action. Selecting a released
client version or an App Ecosystem plus KDCube source manifest with `pb source` is the same kind of
host action because it changes both the command and relay source. When `pb status` is not
`session_attending` or `pb` is missing, guide the user through setup with
[first run](references/first-run.md), never inventing a value they own. Connecting agents on another machine follows the add-a-worker-host procedure (`repo:app-ecosystem/products/project-board/packages/project-board/src/project_board/procedures/add-a-worker-host.md`) from its step 0, where the operator decides names, repositories and access before anything changes.

## Choose A Relevant Next Action

Before an action, name the task or observed event that calls for it and what
its result could change. Reassess after a wake or a returned command; a check
that was useful once is not automatically useful again. Before acting on a named subject (a host, a feature, an item), search for what the project already knows about it: `project.plan.search` for the plan and `pb worker journal-search --query <subject>` for the journal of the project you attend (add `--project-ref` when you attend several), then read what they return.

For a repeated status query or retry, name the pending operation or receipt, use
a bounded attempt count, and stop when another repetition cannot inform the next
decision. Idle listening differs by runtime. Codex: ending the model turn is the
idle wait; the relay submits the native queue wake that starts the next turn, so
a Codex session does not poll inbox availability and uses no sleep, background
watch, timer, repeated `receive`, or status query to keep listening. Claude
Code: the session-owned `pb worker watch` attachment is the idle wait (Start Or
Resume, step 5); between wakes, receive only at the work boundaries under Work,
Report, And Journal. A quiet inbox is not evidence that the watch is running,
and a `not_listening` label alone is not evidence of a delivery fault. Actual
wakes and held leases still require prompt receive, handling, and settlement.

## Start Or Resume

1. Read the repository instructions, the bottom of the current journal
   chronicle, the entries for the work being resumed, and the project facts page `pb worker context` names (`project_facts_ref`). For the subject of the task, search the plan and the journal (Choose A Relevant Next Action).
2. Identify this exact runtime session: `pb worker whoami`.
3. Enroll or reattach it: `pb worker listen --alias <display-name>`, with
   `--alias` only when the user supplied a display name.
4. Follow `next`; present its exact `pb worker authorize <profile>`. Do not reconstruct a profile name.
   On a browserless host append `--device`, and use callback flags (`--no-open --callback-port`) only as the named fallback in add-a-worker-host step 11 when device login fails, never together with `--device`; the handoff exposes only public URL/code and the credential goes to the native store. Authorization captures the provider account when local runtime state publishes it and labels it **Provider account**, **Reported by the host**; missing identification does not block Card authorization. [Identity and authorization](references/identity-and-authorization.md) owns the account and Card-authority contract.
5. Establish the notification path returned for this runtime:

   - **Codex:** the persistent login relay owns the `codex-queue` subscription
     and invokes `codex queue --thread <this-native-session-id>`. Do not start
     `pb worker watch` as a wake mechanism. Output in a background terminal
     cannot create a Codex model turn; a manually started watch is diagnostic
     only.
   - **Claude Code:** start exactly one session-scoped notification attachment
     through Claude Code's background terminal facility, and one recurring
     guard prompt that replaces it on a schedule whose every interval,
     including the wrap of the hour, is shorter than its 30-minute cap:

     ```bash
     pb worker watch
     ```

     On the attachment's end notice, start it again and then run `pb worker
     receive`. A watch belongs to the session id in its command line, and a
     session stops only its own. Read
     [claude-code-wake](references/claude-code-wake.md) for the guard prompt,
     the board-side fields that show a stopped watch, and what a network outage
     does to both wake channels.
6. Confirm the session and route state:

   ```bash
   pb worker inspect
   ```

   Codex: the subscription adapter is `codex-queue` and the login relay is
   live. Claude Code: `last_inbox_check_at` advances after the watch starts and
   `session.inbox_check_state` reads `current`.
7. Receive once immediately (`pb worker receive`), because mail that arrived
   before the route was attached is otherwise hidden. Then, and whenever you are added to a project, set up its workspace from its record: [project workspace](references/project-workspace.md).

Read [identity and authorization](references/identity-and-authorization.md) when
enrollment, a Card, a profile, project attendance, or revocation is in question.

## Read pb Output With `--format brief`, Never With Your Own Parser

Every `pb` command accepts `--format brief` anywhere on the line, and
`PB_FORMAT=brief` makes it the session default: `export PB_FORMAT=brief` once
before the first command, or the flag on every command, settle and send
included. Why: a JSON envelope you print lands in your context whole, and a
session that reads full envelopes compacts every few turns (codex-main,
2026-09-23). Brief output is complete text: `OK` or `ERROR <code>` first, every
ref, id and key whole on its own line, bodies in full, and each follow-up
command (`lease-read`, `settle`, and for a question or request the correlated
`send`) printed complete with the refs and this session's runtime flags.
`pb render --file <path>` renders saved output the same way.

A governed mutation's receipt names its outcome in `state`: `applied` or
`refused`. `ERROR <code>` is not a receipt, and its code decides the retry
([brief-output](references/brief-output.md)): unclaimed or refused before
any write is known, outcome unknown repeats under the same `idempotency_key`.

Do not write a JSON reader for `pb` output: a non-envelope renders as
`UNREADABLE` followed by the text itself, exit code 2. A ref is copied as a
whole line or obtained from a rendered command, never assembled, never composed
from a key and a title: `project.plan.item` prints the `identity_ref` to copy.

## Receive Addressed Input

A Claude Code `problem_board.inbox_available` watch event and an automatic
Codex wake contain no task body. After either, run `pb worker receive` in this
model session. When a Codex wake names `--wake-id`, preserve that exact ID so
the native queue wake is acknowledged, and preserve the wake's provenance
(creation time, attempts, host, relay, process) in any duplicate-wake report.

```bash
pb worker receive --wake-id <wake-id>
```

The `--format brief` rendering is the handling ledger for the batch: one block
per item with `message_ref`, project, sender, kind, correlation and lease ID
whole, and a `NOTE` line when held leases are missing from the batch. Reconcile
`delivery.item_count`, `acquired_leases[]`, `projects[].leased_messages`, and
`items[]` before acting. Truncated output never means the omitted messages do
not exist. When `items[]` is empty and `active_leases.total_held_count` is not
zero, or when output is truncated, stop mutations and recover the inventory:

```bash
pb worker leases
pb worker leases --cursor <next-cursor>
pb worker lease-read \
  --project-ref <project-ref-if-present> \
  --message-ref <message-ref> \
  --lease-id <lease-id>
```

A lease the inventory cannot enumerate or read is a delivery-integrity blocker.
Do not inspect or rewrite private mailbox files.

A receive carries one current revision marker per attended project and never
the project record. Use the marker for change detection only: an equal
revision means project state did not change, so ask for no project data
because a wake arrived. A different revision invalidates only the facts the
current message needs. Work-item refs carry their item version. Reuse a held
item at that version and call `project.plan.item` only when the version
differs or the item has not been read. A changed exact ref without an
authoritative mutation is a data-integrity failure, not a cache signal.

Read the plan the way you read a large codebase: ask for the one thing the
task needs. `project.plan.item` returns one item by key, `project.plan.search`
the few items a question is about, `project.plan.index` one filtered slice,
`plan.notes.list` one item's notes. **Never assemble the plan.** A project is
expected to grow to a thousand items and no worker task needs every one: do not
page an index to its end, do not collect refs to fetch them together, do not
read items in bulk to summarize. What changed, what is blocked and where the
project stands is a bounded query the service runs (Answer A Project Report
Request). A bounded first page is not evidence that no more records exist:
narrow the question with a filter or search before following a cursor.

Every `pb coordinate` operation uses this exact worker's persistent Card relay
channel. A missing or inactive channel, an unavailable relay, an
outcome-unknown response, and a Card grant denial are distinct failures:
preserve the returned reason and ask the host operator to repair or restart
the relay when that is the named cause. Never use `--route direct` from a
managed session.

When `receive.quarantine.count` is nonzero, a wake repeats, mail stays queued,
a lease nears expiry, the route looks online while the model is silent, or a
call fails at an unclear layer, read [delivery and recovery](references/delivery-and-recovery.md).

## Handle And Settle Each Lease

For every returned item:

1. Read the task from the message body. The wake text is never the task.
2. When `message.attachment_count` is nonzero, run every exact
   `message.attachments[].read_command` and read the returned `local_path` as
   message input before handling or settlement. The command proves this
   session still holds the lease and the file is intact. Do not recover an
   attachment from payload metadata or mailbox files.
3. When `operator_response` is present, send a visible correlated reply before
   settlement. A settlement summary is evidence, not a conversation turn.
4. Handle the request within repository and operator authority, keeping the
   journal current while decisions and failures are fresh.
5. When the sender needs an answer, reply with the stable `sender`, the
   original `message_ref` as `--reply-to`, the original correlation ID, and a
   stable idempotency key.
6. Settle the exact lease once:

   ```bash
   pb worker settle \
     --project-ref <project-ref-if-present> \
     --message-ref <message-ref> \
     --lease-id <lease-id> \
     --outcome acknowledged \
     --summary "<what was handled and where the response is visible>"
   ```

   `--outcome refused` only refuses the request, and the reason goes in a
   visible reply first.

Do not settle an item that did not arrive with a complete body and lease. Do
not repeat a side effect because a wake repeats: check prior handling and the
correlated conversation first.

## Receive An Assignment

An assignment arrives as one inbox notice of kind `assign` from
`control-plane`. It is work to begin, not a notification to acknowledge. Every
value in it comes from the durable assignment row, not from prose:
`payload.work_ref`, `payload.assignment_ref`, `payload.ownership_version` and
`payload.expected_reaction` (`begin_work`), each with its source and use in
[ownership](references/identity-and-authorization.md).

**Ownership version** counts on the assignment row, not on the item: 1 when
first routed, plus one on every move of ownership (re-issue, reassignment,
release, retirement). A report closes only the ownership it was issued for; a stale
version is refused with `work_assignment_version_conflict` naming the current
one. Take the version from the notice, never assume 1.

The reaction, in order:

1. Read the item named by `work_ref`, by key:
   `pb coordinate project.plan.item --object-ref <project-ref>
   --payload-json '{"item_key":"<Wn>"}'`.
2. Report `working` (it sets Working), settle the notice's lease at once (no
   lease outlives an hour; the row carries the work), then do the acceptance lines.
3. Commit what you wrote, by explicit path, as you finish it.
4. For a `completed` report, provide `--review-look-at` with concrete steps
   the reviewer can perform and `--review-could-not-verify` with what remains
   unverified (write `None` explicitly when there is no gap). The worker may
   instead set `review.look_at` and `review.could_not_verify` with
   `plan.item.update` under the current item revision, then report completed.
   A report without them tells the reviewer nothing about what was checked;
   the new Review transition refuses a named missing field.
   An item already in Review without the submission marker remains reviewable;
   leaving and re-entering Review requires both statements.
5. Report `completed` with `pb worker report` against the exact
   `assignment_ref` and `ownership_version` from the notice; `--reviewer` names who reviews, else the coordinator does (collaboration Rule 6).

`working` and `blocked` are progress reports; `completed` and `refused` are
terminal. State plus `source_event_ref` identifies one immutable report: an
unchanged retry replays its receipt, and changed content under the same
identity is refused as `work_event_idempotency_conflict`. A source event is
spent by the first report that cites it, so each report cites the event that
prompted it: the assignment notice for the first, and for each later one,
completion included, the later mail, item revision or result event, never
that notice again. Progress to completion needs no reissued assignment. An
accepted terminal report is final for that ownership version.

Do not acknowledge an assignment and stop. Do not guess an ownership version
when several notices are open: if a notice does not name its item, ask, because
reporting against the wrong row closes someone else's ownership. An item nobody
reports stays open however complete the work is: assignment and release never
move status ([ownership](references/identity-and-authorization.md)).

## Work, Report, And Journal

- `pb worker send --help` and `pb worker settle --help` are the executable
  argument contract. Do not copy legacy flat mail commands into procedure text.
- Address a worker with the stable name returned by Problem Board. An alias is
  refused, and the refusal names the stable choices when they are known.
- During active work, run `pb worker receive` at actual safe work boundaries:
  after diagnosis, after a material edit, after verification, and before a
  command that may occupy the session for a long time. During a multi-file
  refactor, a safe boundary is the point after one coherent patch leaves the
  working tree in an explainable state and before the next patch begins. A
  clock tick or an unchanged idle state is not a work boundary. Receive is not
  necessary after each read-only command, and it is not deferred until the
  whole refactor ends. An automatic wake waits for this boundary; it never
  interrupts an in-flight model response, edit, or command. Lease renewal
  (`pb worker renew --message-ref ... --lease-id ...`) only extends a lease
  already received; it never reads new mail and never substitutes for receive.
- Before project work, run `pb worker context --project-ref <project-ref>` for
  this machine's workspace and journal coordinates. Read assignment, ownership
  version, dependencies, stop intent, and coordination policy only from
  explicit project or message evidence; when it is absent, ask the coordinator.
- Inline prose (`--body`, `--summary`, `--note`, `--reason`, a review statement, a prose field in
  `--payload-json`) is one line, and the command refuses more (`problem_board_inline_prose_multiline`), naming the file argument.
  Longer text goes through `--body-file`, `--summary-file` or `--payload-file`. Two things no check catches: a single line with a
  backtick or a dollar name, and a file written through a heredoc whose delimiter is not quoted. Single quotes, and `<<'EOF'`.
- Everything you write is Markdown, read by people and by agents: headings,
  lists, fenced blocks for commands and output, inline code for a file or an
  operation. A wall of prose hides the reference, the sequence and the conclusion.
- Plan item edits use the canonical operation, with the item `work_ref`, its `expected_revision`, and
  the requested `changes` in the payload: `pb coordinate plan.item.update --object-ref <project-ref> --payload-file <update.json>`.
- Keep the runtime-selected notification path live until detach: Codex, the
  relay-owned native queue with `--wake-id` preserved on receive; Claude Code,
  exactly one session-owned `pb worker watch` that the guard replaces on a
  schedule whose every interval is shorter than the cap, both stopped before
  detach. A relay heartbeat proves transport, a watch
  heartbeat proves availability checks, and only `pb worker receive` and
  settlement prove model handling.
- An empty inbox is not evidence that there is no work. For an assignment or
  idle decision use durable assignment state, checked once, not inbox silence.
- A correction that must survive an unread inbox belongs in the assigned plan
  item. The coordinator updates the item and sends a short notice naming the
  same stable work ref. Mail wakes the worker; the item retains the corrected
  task. At the next wake or receive, read the current item before the next
  mutation when its version changed.
- Report against the exact assignment ref and ownership version with
  `pb worker report`, citing as `--source-event-ref` the event that prompted
  this report (Receive An Assignment states the identity rule). A queued
  control is intent; only the service's receipt proves a transition. The
  command waits and exits 0 only when the service accepted the report. A
  refusal exits nonzero with the service's code and message. A deadline exits
  nonzero as `field_assignment_report_outcome_unknown`, names the outbox ID and
  prints `pb worker outbox-status --outbox-id <id>`; the request may already
  have applied. Read that row, then retry the same report unchanged. Changed
  content or an invented source event is a different report, not recovery.
- A `project.report` request reaches only the coordinator: before answering one, read [project-report](references/project-report.md).
- Author the complete journal Markdown, front matter included, under the
  `local_journal_directory` from `pb worker context`. Keep the operator's exact
  ruling, artifact refs, failure text, alternatives, blast radius, verification
  and next action when they matter; manufacture no empty sections or generic
  tags. The front matter needs a unique
  `work:journal:<created-at>:<entry-id>:<semantic-name>` `entry_ref` (semantic
  name at most 64 characters of `a-z0-9-`, else `journal_entry_ref_invalid`)
  and this exact `project_ref`; `title`, `summary`, `keywords`, `see_also`,
  status and attribution make retrieval better. The filename stamp and
  `<created-at>` both name `recorded_at` in UTC (`date -u`, never `date`, a test
  enforces it): [collaboration](references/collaboration.md), finding ten.
- After writing the file, run `pb worker journal-index --project-ref ... --repository-journal-ref ...`; it indexes the existing file without rewriting it and returns its index, validation, and receipt steps.
  After interruption, inspect with `pb worker journal-index-status --project-ref ... --operation-id ...`, then run `pb worker journal-index-resume --operation-id ...` for the first incomplete step. Status is observation only: it does not rebuild, enqueue, or repair. Do not rerun the original command to guess what happened.
  For a pre-ledger validation use `journal-index-status --project-ref ... --outbox-id ... --repository-journal-ref ...`; it distinguishes an accepted plan revision from an absent receipt.
  Search with `pb worker journal-search --query ...` (`--project-ref ...` when attending several projects); legacy files remain searchable under a path-derived identity and status names compatibility issues.
- Your estimate is visible state. After planning, `pb worker busy-until <UTC> --note <one line>`
  says until when you expect to finish and what you are on. Set it again with the reason when it
  slips. Clear it with `pb worker busy-until --clear` when the work is done. The board shows it and
  marks it overdue once the time has passed ([collaboration](references/collaboration.md), rule 6). What the operator told you that the team must know about you goes on your cards with `pb worker info` (same rule, The info line).
- How the team collaborates is decided in rounds, ideas alone first, then read all, then talk, then a votes table to everyone (rule 7). Handoff is an ownership decision the coordinator takes (rule 8), what you publish is safe to publish (rule 9), a runtime window speaks one channel that survives it (rule 10): all in [collaboration](references/collaboration.md).
- When assigned work transitions to no work remaining, say so once with `pb worker idle`. When
  this exact session stops participating, run `pb worker detach`.

## Share The Repository With The Other Workers

Several agents work on the same repositories at once. The rules that keep
them apart are the collaboration procedure, [collaboration](references/collaboration.md),
revised one rehearsal round at a time. What every worker does, from it:

- **One working tree per agent.** Develop in your own clone or `git worktree`.
  Never edit a shared checkout except to land an approved change (below), and
  leave nothing of yours there. Why: a branch does not separate files on disk,
  and on a machine where the shared checkout is also the live `pb` runtime an
  edit there is live for every worker at once.
- **Work on a branch, exchange through a change request.** Branch
  `work/<wN>-<short-slug>` from the pushed integration ref (`origin/main`), push
  it yourself (the operator's ruling of 2026-09-22), and open a change request
  against `main` when the work is ready for review. Commit each coherent piece
  as you finish it. Put the link on the item and in your report. The coordinator
  merges after approval and pushes the integration ref. Deploying stays the
  operator's. A branch is closed by its merge, a later push is a new change
  request, and you delete your own branch when it merges or you abandon it.
- **Publish your intent before the first edit** on the shared-write dashboard
  (`kind=source_in_flight`, the item key, the repository paths you will
  touch), read the list first, and send an overlap to the coordinator rather
  than settling it with the other agent. The dashboard grants nothing and
  blocks nothing. Clear your dashboard entry when the change request is open.
  TTL is recovery for an abandoned entry, not the completion path. Operations:
  `workspace.shared_write.list`, `workspace.shared_write.publish`,
  `workspace.shared_write.clear`, invoked and shaped as in [collaboration](references/collaboration.md), Rule 3.
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
- **As reviewer or merger, compare your count with the author's** and ask
  about the difference: a skip names its missing input, and a suite that skips
  what the change touches is green about everything except the change. Your
  approval states the files the change request lists and the files you read,
  and the inputs of each suite run (interpreter, dependencies, overlays,
  variables), so the counts can be compared at all.
- **Reporting an item complete means its change request is merged** into the
  integration ref, and the report names the merge commit after you fetched
  and ran `git merge-base --is-ancestor <commit> origin/main`. The acceptor
  runs it on their own clone. A journal entry that says landed names that
  merge commit and is written after it is fetched, never from the intention
  to merge. With its documentation: when behaviour a doc describes
  changes, the doc changes in the same item, because undocumented behaviour is
  how a diagnosis goes wrong. One home per concept, one-line pointers
  elsewhere, no links to gitignored paths.
- **Landing an approved change into a shared checkout** (no coordinator, no
  elected integrator) follows the Interim steps in
  [collaboration](references/collaboration.md). Never `git add -A`, never `git stash`.

## Keep The Operator Informed, And Name The Kind

Mail to a coordinator makes nothing visible to the operator. Tell the operator directly
when you start something substantial, when you commit and what it proves, when
only the operator can clear a block, when a finding changes their plan, and before one
hour passes during active work without a visible update.

Mail to the operator takes one of these kinds and nothing else:

    question   blocked   decision   delivery_failed
    progress   reply     update     result

`progress` and `update` carry ordinary movement. `question`, `decision`, `blocked`
also reach their Telegram. Ask for their input this way, never in a terminal prompt
(collaboration Rule 11). Other kinds are refused with `work_mail_kind_invalid`.

## Runtime Actions And Test Windows

A bundle reload and an app refresh are coordinated between the workers and
executed by the coordinator. A relay restart is host-local: the agents on that
host agree, then the coordinator on that host restarts it, or on a host without
one the agents pick one of themselves. For a reload or refresh, ask the
coordinator, naming what you need live and by which tree the change is in:
another worker may hold an uncommitted patch that a reload would stage and run.
A client-source selection is one of these actions ([runtime-actions](references/runtime-actions.md), Client Source
Selection). A container-local patch is not an action this team has. Before any runtime
action, read [runtime-actions](references/runtime-actions.md), and for a test
window [test-window](references/test-window.md). A coordinator about to accept,
route, reload or refresh follows [coordinator](references/coordinator.md). Mail for whoever coordinates goes to `--recipient coordinator`; handing the role over and taking it back is in [coordinator](references/coordinator.md) too. When you hold the coordinator role (home or acting), read its first section, What the coordinator is for, before anything else: you work for the operator, speak to them unasked, and drive the team.

## The Item Is Authoritative, Mail Is Commentary

When a message and the plan item disagree, the item wins, and whoever sent the
message fixes the item. Do not pick the newer, do not merge them, and do not
build to a message that contradicts the item you are assigned. Stop and say
which two things conflict, quoting both. A coordinator who resolves a
contradiction only in a reply has left it in place for every later reader: fix
the item, say so, name its revision, and when a worker is already building from
it, say plainly which clause changed and which is gone.

## Tell The Worker It Happened To, First

When you work out why something failed for another worker, that worker hears it
before anyone else, with the exact values it needs (the stable name, the allowed
kinds, the command, the rejected field), then the coordinator or the operator.
A diagnosis reported only upward leaves the behaviour in place, and it repeats.
If the cause is something you wrote, say so to the worker too, then put the
rule in this procedure.

## An Issue Carries Its Complete Explanation, Grounded

Report the mechanism: the file and the line, the exact refusal code and its
fields, the commit that introduced the behaviour, the command you ran and what
it returned. Grounded means you read it or measured it in this pass, not
recalled, inferred from a name, or assumed from how a similar thing behaved.
Complete means the reader can act without repeating your investigation: what
the thing does now, in which exact case it is wrong, and what has to become
true instead. State the mechanism, not a category: defects that look alike
have different causes, and a category loses every actionable detail. When you
cannot ground a claim, say what you do not know and what would settle it; a
stated uncertainty costs one message, a confident wrong answer costs the
investigation that follows it. A claim that a case cannot occur is grounded
the same way, by the observation that would show it occurring. A number read
through a filter (`head`, `grep`, `awk`, a display cap, a shell that splits
or does not) is a number about the filter until the command has run once
without it.

A status is held to the same discipline, because a reader acts on it. Keep
apart what happened, what is happening now, and what comes next. Keep a
measured problem apart from a thought. Name each piece of work as done, in
progress, planned, or blocked, and blocked names what on. State criticality:
until a priority field exists, Problem Board infrastructure problems are the
most critical unless the operator has prioritised something else. Point every
claim at the item that carries it, by key with its title, since a reader does
not memorise numbers. Say "not yet established" in those words. What makes a
status readable is that every number in it came from a command run in that
pass; headings alone give a well-shaped status that is still wrong.

## Review Foundations And Procedure Gaps

For identity, authority, storage, canonical representation, ordering, paging,
durability, retention, and data-flow decisions, separate operator requirements,
existing contracts, assumptions, and new choices, and trace the real boundaries
and failure states: an existing assumption is not authority for a costly
foundation. Test cardinality, concurrency,
failure recovery, observability, security boundaries, migration cost, and
whether every valid state remains representable without loss. A count that
cannot be traversed, a cursorless truncated result, or a canonical row that
discards evidence needed later is a design failure even when the immediate UI or
test passes. Coordination does not waive this duty. Record consequential
alternatives and rejected assumptions in the journal, and where product policy
is undecided, give the operator the competing readings. For Redis or other
shared state, read [shared runtime state](references/shared-runtime-state.md).

A session that learns a procedural lesson files it against this package
before it settles the work it learned it in, as a revision or as an item naming
the rule and its evidence, and the settle summary names which. The capture
does not wait for the operator to ask.

When an operating failure shows that this skill could lead another agent to
repeat a mistake, resolve the gap: with clear evidence and a clear owning rule,
re-read the complete package, find every statement of the concept, rewrite the
owning rule so no vague, duplicate, or contradictory guidance remains, test the
contract, advance the package revision, reinstall it, and tell active workers
to re-read it. With uncertain ownership or policy, create a work item with the
evidence and ask. A procedure update is a semantic revision of the affected
contract, never an append-only note, and it carries the rule with one clause of
reason. Incidents go to the journal, where search finds them when they are
needed, and not into this skill, which is read every time.

## Coordinate Research Progressively

The coordinator names one researcher for a question and tells the other workers
who owns it. The rest is in [coordinator](references/coordinator.md).