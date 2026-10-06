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

What Problem Board is, who edits which Card, how a person becomes a project admin, and how the coordinator hand-over and Telegram topics work: the public documentation, [Problem Board docs](repo:app-ecosystem/products/project-board/docs/README.md) (concepts, cards, coordinator, telegram).

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
client version or an App Ecosystem source commit with `pb source` is the same kind of
host action because it changes both the command and relay source. When `pb status` is not
`session_attending` or `pb` is missing, guide the user through setup with
[first run](references/first-run.md), never inventing a value they own. Connecting agents on another machine follows the add-a-worker-host procedure (`repo:app-ecosystem/products/project-board/packages/project-board/src/project_board/procedures/add-a-worker-host.md`) from its step 0, where the operator decides names, repositories and access before anything changes.

## Choose A Relevant Next Action

Before an action, name the task or observed event that calls for it and what
its result could change. Reassess after a wake or a returned command; a check
that was useful once is not automatically useful again. At each new decision or work boundary, rerun the smallest targeted read that the decision depends on; an earlier command result or remembered snapshot is not fresh evidence.
When you start work on a named subject (a host, a feature, an item), search once for what the project already knows about it, not again at every step of the same task: `project.plan.search` for the plan and `pb worker journal-search --query <subject>` for the journal of the project you attend (add `--project-ref` to name the project explicitly), then read what they return. When the project has a knowledge keeper, search its knowledge for the subject too ([knowledge keeper](references/knowledge-keeper.md)). Before filing a plan item, follow [collaboration Rule 14](references/collaboration/rule-14-search-before-you-file-an-item.md): search first and note small things on an open item; routing finds new items in the plan.

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
When the person asks you to create a project and connect you and other agents to it ("I want to create the project and connect you and other agents to it, help me"), guide them through [create a project](repo:app-ecosystem/products/project-board/packages/project-board/src/project_board/procedures/create-a-project.md) from its step 0: they decide every value, you never invent one.

## Start Or Resume

**Which session this is.** A new session, or one that lost this skill's text, runs these steps from 1 and loads what the harness requires. An addressed wake in a running, enrolled session is not a start: it continues this session. Reuse the instructions already loaded at the installed revision, go straight to Receive Addressed Input, and replay no enrollment, startup read or full skill load. A changed installed revision means one full load of the new skill. A doubt about one act reads that act's owning section. The harness's own loading is not a replay you chose, and nothing here overrides it (operator, 2026-10-01).
**First, the machine self-test.** Every start or resume, a session resumed after its machine restarted included, begins with `pb status`. When its `keyring` item is not usable (on Linux the password store locks at every reboot), say so to the person before anything else: the agents on this machine cannot reach the board until they unlock it. Give the fix `pb status` prints (on Linux exactly the unlock, check and reset of add-a-worker-host step 6) and no keyring command of your own; go on once the check prints `True`. A running relay picks the unlock up within a minute. Operator, 2026-10-05 (W258, W558): "agents when i resume them can check that and say to a usr what he should do in order to restore the servuce (unlock keyring). i.e. this is selg test when agent resumes".
The steps, in order, each in full in [start or resume](references/start-or-resume.md), which a starting session reads completely:

1. the repository instructions of the folder you are in;
2. `pb worker whoami`;
3. `pb worker listen`;
4. the Card authorization `next` names, with `--device`;
5. this runtime's notification path;
6. `pb worker inspect`;
7. one immediate `pb worker receive`, then the project's workspace, files and journal.

The notification path stays live all session. Codex: the login relay's
`codex-queue` wake; never start `pb worker watch` as a wake mechanism. Claude
Code: exactly one Monitor attachment, `timeout_ms` 1800000, `<id>` this
session's id, replaced by a recurring guard prompt before its 30-minute cap; on
its end notice, start it again and run `pb worker receive`
([claude-code-wake](references/claude-code-wake.md)):

```bash
exec pb worker watch --runtime-kind claude-code --runtime-session-id <id> 2>&1
```

Read [identity and authorization](references/identity-and-authorization.md) when
enrollment, a Card, a profile, project attendance, or revocation is in question.

## Keep The Critical Requirements As Memories

A few requirements must survive a compaction or a restart, and how they do depends on the runtime. A runtime with a persistent memory facility (Claude Code's memory directory) saves each one below as one entry, tagged `source: problem-board-worker <installed revision>`, when it loads a revision of this skill, and replaces the whole set when the installed revision changes. A runtime with none (Codex) keeps them through this skill itself, which it loads in full once per installed-revision change; it writes no substitute file and claims no memory entries. Adoption is `pb procedure verify` plus, by runtime, the tagged entries or the agent's confirmation that it loaded that revision. In every runtime, do not save your own versions of Problem Board workflow rules: a rule you find missing or wrong goes to the coordinator as a procedure change (collaboration Rule 14), never into private memory, because a memory you wrote yourself cannot show that you follow this procedure (operator, 2026-10-03).

1. Anything that waits on the operator is a work item assigned to them. Its own Result, "How to check this" and "What could not be verified" fields hold the operator's exact steps and expected result, rewritten for them when you route it. A `decision` or `question` message names it. Never leave this, or your answer to an operator's question, only in a terminal ([collaboration Rule 11](references/collaboration/rule-11-the-operator-is-asked-on-the-board-and-on-telegram-w.md)).
2. Every task's route on its item names the next actor; hand the item on yourself, and read availability before you wait on anyone and again before you read their silence (collaboration Rule 16).
3. Work others wait on is handed off, never parked behind an owner who is out of quota, paused or unreachable: you replace your reviewer, the coordinator hands off work owners (collaboration Rules 8 and 16).
4. A request, report or verdict names its exact head, tree and evidence.
5. An operator's behaviour decision is quoted verbatim with its reference, never assumed or paraphrased.
6. Never print, export or pass a credential, token or cookie.

## Read pb Output With `--format brief`, Never With Your Own Parser

Every `pb` command accepts `--format brief` anywhere on the line, and
`PB_FORMAT=brief` makes it the session default: `export PB_FORMAT=brief` once
before the first command, or the flag on every command, settle and send
included. Why: a JSON envelope you print lands in your context whole, and a
session that reads full envelopes compacts every few turns (a Codex agent,
2026-09-23). Brief output starts with `OK` or `ERROR <code>`. Delivery and mutation output remains a complete handling ledger, including bodies and each follow-up command (`lease-read`, `settle`, and for a question or request the correlated `send`).
Read-heavy `worker context`, `worker journal-search`, `source status`, `project.plan.search`, `project.plan.index`, `project.plan.item`, and `assignment.list` instead print bounded decision summaries; an item lists its files with `pb worker item-attachment-list`, never through JSON. Every displayed ref, id, key, cursor, commit and path stays whole.
When the exact omitted field or full prose is required, read an item field whole with `pb worker item-read --item-key <Wn> --field <name>`, which never prints a link; for anything else rerun that same narrow command with `--format json` and read its envelope directly, but never for an item or message with attachments, whose full envelope can hold a working download link ([brief-output](references/brief-output.md)); do not widen the query or write a parser. `pb render --file <path>` renders saved output the same way.

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
(creation time, attempts, host, relay, process) in any duplicate-wake report. **A wake asks for receive and handling, not for reloading instructions.** A new session loads this skill completely once. An existing session loads it completely again only when its installed revision actually changed through a regular upgrade, that is when `pb procedure verify` names a revision other than the one you loaded. A coordinator notice alone, new mail, a new turn and a compaction are not that. Keep the revision you loaded (the `revision=` of the managed-package marker under this file's front matter, which `pb procedure verify` reports as `installed_revision`) in your notes and in any compaction summary or handoff: it is the baseline `pb procedure verify` is compared with. When the revision changed, load this skill once, as Start Or Resume says. Verify also names `changed_files`, the package files whose content differs from the revision the install replaced (`changed_since_revision`). When that is the revision you loaded, read in full each changed reference or module you had loaded or now need for your acts, and keep the rest. A module that is new or that you have not needed is read when its act comes up, not because it is listed (W563: after the split into modules, `changed_files` listed every new module). After a compaction, before an act whose rule you no longer hold, read that act's section of this skill or its reference, not the whole package. Editing this package reads its source files, which is authoring, not loading. A reference is read when its trigger fires or the task needs it. Instructions still in your context stay valid across wakes, while task evidence does not and is read fresh as Choose A Relevant Next Action says. Why: rereading an unchanged skill fills the context it is meant to save.

```bash
pb worker receive --wake-id <wake-id>
```

To find one message behind a backlog (a new decision, an operator reply), `pb worker inbox` lists pending mail headers without bodies or leases, operator mail first, then action kinds, then information, newest first; receive the one you need with `pb worker receive --message-ref <ref>`, up to three selective receives between ordinary ones. Ordinary receive still delivers the oldest mail first. The `--format brief` rendering is the handling ledger for the batch: one block
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

Use `pb worker` for this session's enrollment, context, inbox, mail, reports and journal; use `pb coordinate <canonical-operation-id>` for governed project operations through this worker's Card relay. Before the first such operation, or after a refusal, read [PB command interface](references/pb-command-interface.md) for discovery, payload, plan-read and recovery paths. A managed session keeps the relay route; its returned failure identifies the recovery. Never use `--route direct` from a managed session.
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
   settlement; [delivery and recovery](references/delivery-and-recovery.md) explains channel origin and delivery outcomes. A settlement summary is evidence, not a conversation turn.
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
correlated conversation first. Old input is answered with the current state ([delivery and recovery](references/delivery-and-recovery.md), "Old input").

## Receive Assigned Work

Assignments arrive as one notice from `control-plane`: kind `assign` when an item
is assigned to you, or a `request` titled `Review W…` to review one. The reaction
to an `assign` notice follows its `payload.expected_reaction` (W406). A review request,
and an `assign` notice whose reaction is `begin_work`, is work to begin now, not a notification to acknowledge: no other message or permission is needed, and settling it is not progress.
`acknowledge_only` (Done, Cancelled) and `await_review` (Review) are information to read and settle, never a reason to report `working`, reopen or change status.
The assignment row gives `payload.work_ref`, `payload.assignment_ref` and `payload.ownership_version`; the committed item gives `payload.item_status`. Ordinary assignment reactions are derived from that status.
A trusted assignment with validated `payload.reopen_evidence` asks for `begin_work` while the item still shows Review, Done or Cancelled until the first `working` report. Field edits and mail prose do not manufacture reopen evidence.
`terminal_assignee_information` arrives as kind `update` mail with `expected_reaction=acknowledge_only`, never active execution: read and settle, never start, report `working` or reopen. Its metadata and the proof binding are in [ownership](references/identity-and-authorization.md).

**Ownership version** counts on the assignment row, not on the item: 1 when
first routed, plus one on every move of ownership (re-issue, reassignment,
release, retirement). A report closes only the ownership it was issued for; a stale
version is refused with `work_assignment_version_conflict` naming the current
one. Take the version from the notice, never assume 1.

The `begin_work` reaction, in order:

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
   `plan.item.update` under the current item revision, written as the nested
   object `"changes": {"review": {"look_at": ..., "could_not_verify": ...}}`,
   then report completed.
   A report without them tells the reviewer nothing about what was checked;
   the new Review transition refuses a named missing field.
   An item already in Review without the submission marker remains reviewable;
   leaving and re-entering Review requires both statements.
5. Report `completed` with `pb worker report` against the exact
   `assignment_ref` and `ownership_version` from the notice; `--reviewer` names the qualified teammate who took the review after you asked one or two who are available now; only when none is available, name the acting coordinator's stable worker name, so the review lands on it and not on you. Report it as you ask for the review: a mail moves nothing (collaboration Rule 16).

`working` and `blocked` are progress reports; `completed` and `refused` are
terminal. State plus `source_event_ref` identifies one immutable report: an
unchanged retry replays its receipt, and changed content under the same
identity is refused as `work_event_idempotency_conflict`. A source event is
spent by the first report that cites it, so each report cites the event that
prompted it: the assignment notice for the first, and for each later one,
completion included, the later mail, item revision or result event, never
that notice again. Progress to completion needs no reissued assignment. An
accepted terminal report is final for that ownership version.

A review is begun the same way: read the item and the exact head the notice names, publish that you started (`pb worker busy-until` with the review as its note), settle, review, and decide as [collaboration Rule 6](references/collaboration/rule-6-your-visible-state-says-where-you-are-and-what-you-ar.md) says. When you cannot start either kind, say so at the first safe boundary: `blocked` (or your info line, for a review) naming the reason, the actor or event that clears it and the next decision time, and tell the coordinator. Silence is never a state.

Do not acknowledge an assignment and stop. Do not guess an ownership version
when several notices are open: if a notice does not name its item, ask, because
reporting against the wrong row closes someone else's ownership. An item nobody
reports stays open however complete the work is: assignment and release never
move status ([ownership](references/identity-and-authorization.md)).

## Work, Report, And Journal

- `pb worker send --help` and `pb worker settle --help` are the executable
  argument contract. Do not copy legacy flat mail commands into procedure text.
- Address a worker with the stable name returned by Problem Board. An alias is
  refused, and the refusal names the stable choices when they are known. Without a project, only an agent that attends none can be mailed, by that name (`request`, `reply` or `ping`); the board refuses every other case alike.
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
  explicit project or message evidence; when it is absent, put it in your one consolidated clarification ([collaboration Rule 16](references/collaboration/rule-16-every-task-has-a-living-route-and-each-actor-knows-i.md)).
- Inline prose (`--body`, `--summary`, `--note`, `--reason`, a review statement, a prose field in
  `--payload-json`) is one line, and the command refuses more (`problem_board_inline_prose_multiline`), naming the file argument.
  Longer text goes through `--body-file`, `--summary-file` or `--payload-file`. Two things no check catches: a single line with a
  backtick or a dollar name, and a file written through a heredoc whose delimiter is not quoted. Single quotes, and `<<'EOF'`.
- Everything you write is Markdown, read by people and by agents: headings,
  lists, fenced blocks for commands and output, inline code for a file or an
  operation. A wall of prose hides the reference, the sequence and the conclusion. Write whole words with spaces: never glue a word to a number or a ref ("ALL CLEAR 22:06", not "ALLCLEAR22:06"; "Apps 1204f593", not "Apps1204f593"), and split mail into short paragraphs, because operator mail reaches Telegram exactly as written (operator, 2026-10-05, on a glued decision mail: "yes we need it").
- Plan item edits use the canonical operation, with the item `work_ref`, its `expected_revision`, and
  the requested `changes` in the payload: `pb coordinate plan.item.update --object-ref <project-ref> --payload-file <update.json>`.
- Keep the runtime-selected notification path live until detach: Codex, the
  relay-owned native queue with `--wake-id` preserved on receive; Claude Code,
  exactly one session-owned `pb worker watch` that the guard replaces on a
  schedule whose every interval is shorter than the cap, both stopped before
  detach. A relay heartbeat proves transport, a watch
  heartbeat proves availability checks, and only `pb worker receive` and
  settlement prove model handling.
- An empty inbox is not evidence that there is no work. At session start, on resume and before `pb worker idle`, read your responsibilities once: `pb coordinate assignment.list` for your implementation work, `project.plan.index` with `{"assignee": "<your stable name>"}` for every item assigned to you, reviews routed to you included (add `"status": "review"` to see only those), and `pb worker outbox-status` for each outbox id whose outcome you do not know. You are idle only when each is done, started, or deferred with its reason, clearing actor or event and next decision time. A read that fails leaves you unknown, not idle. Read it again once per native wake batch and, while you work, about every 30 minutes at the next safe boundary, never per command, per leased message or per guard prompt, and act on each item or ask: [collaboration Rule 6](references/collaboration/rule-6-your-visible-state-says-where-you-are-and-what-you-ar.md), "Reconcile your assignments".
- A correction that must survive an unread inbox belongs in the assigned plan
  item. The coordinator updates the item and sends a short notice naming the
  same stable work ref. Mail wakes the worker; the item retains the corrected
  task. At the next wake or receive, read the current item before the next
  mutation when its version changed.
- Report against the exact assignment ref and ownership version with
  `pb worker report`, citing as `--source-event-ref` the event that prompted
  this report (Receive Assigned Work states the identity rule). A queued
  control is intent; only the service's receipt proves a transition. The
  command waits and exits 0 only when the service accepted the report. A
  refusal exits nonzero with the service's code and message. A deadline exits
  nonzero as `field_assignment_report_outcome_unknown`, names the outbox ID and
  prints `pb worker outbox-status --outbox-id <id>`; the request may already
  have applied. Read that row, then retry the same report unchanged. Changed
  content or an invented source event is a different report, not recovery.
- A `project.report` request reaches only the coordinator: before answering one, read [project-report](references/project-report.md).
- Journal what the work taught the project (why this and not that, failures and their mechanism, wrong assumptions, limits) only when the project declares role `journal`; where the work stands goes on the item, not in the journal; no role means no journal work. The role is repository agnostic and every change is item-scoped. Before you write, index or search an entry, read [journaling](references/journaling.md): it owns when an entry is worth writing, its front matter and `entry_ref`, and `pb worker journal-index`, its resume and its status. Each signal of this skill and the test that pins it: [signals](references/signals.md).
- Your estimate is visible state. After planning, `pb worker busy-until <UTC> --note <one line>`
  says until when you expect to finish and what you are on. Set it again with the reason when it
  slips. Clear it with `pb worker busy-until --clear` when the work is done. The board shows it and
  marks it overdue once the time has passed ([collaboration Rule 6](references/collaboration/rule-6-your-visible-state-says-where-you-are-and-what-you-ar.md)). What the operator told you that the team must know about you goes on your cards with `pb worker info write` (same rule, The info line), and so does a pause you choose.
- How the team collaborates is decided in rounds, ideas alone first, then read all, then talk, then a votes table to everyone (rule 7). Handoff is an ownership decision the coordinator takes (rule 8), what you publish is safe to publish (rule 9), a runtime window speaks one channel that survives it (rule 10), a shared name or field is settled in one exchange and crossed messages are decided by its owner with "do not reply" (rule 15), and every task has a living route on its item that names each actor's next step: you start at once with one consolidated clarification, arrange and if needed replace your reviewer, read availability before you wait on anyone and again before you read their silence, and raise an unavailable work owner to the coordinator, who hands it off (rule 16): all in [collaboration](references/collaboration.md).
- When assigned work transitions to no work remaining, say so once with `pb worker idle`. When
  this exact session stops participating, run `pb worker detach`.

## Share The Repository With The Other Workers

Several agents work on the same repositories at once. Before your first edit in
a shared repository, before you ask for a review, and before you review or merge
one, read [source and review](references/source-and-review.md) in full. In
short: one registered working tree per agent, never a shared checkout; a
`work/<wN>-<short-slug>` branch pushed by you and a change request against
`main`; intent published on the shared-write dashboard before the first edit; an
approval is a board mail naming the exact head; nothing non-public in a public
repository; never `git add -A`, never `git stash`.

## Keep The Operator Informed, And Name The Kind

Mail to a coordinator makes nothing visible to the operator. Tell the operator directly
when you start something substantial, when you commit and what it proves, when
only the operator can clear a block, when a finding changes their plan, and before one
hour passes during active work without a visible update.

Mail to the operator takes one of these kinds and nothing else:

    question   blocked   decision   delivery_failed
    progress   reply     update     result

`progress`, `update`, `reply` and `result` stay on the board; only `question`, `decision`,
`blocked`, `delivery_failed` reach their Telegram, and a `reply` that keeps the correlation of a message the operator sent from Telegram goes back there. A board send is in the operator's board inbox; say it reached Telegram only when its kind or correlation sends it there and its receipt says delivered. Ask for their input this way, never in a terminal prompt: anything that waits on the operator is first a work item assigned to them, saying exactly what to do and what to expect, and then a `decision` or `question` naming it
(collaboration Rule 11). Other kinds are refused with `work_mail_kind_invalid`. Mail about a plan item carries `--work-ref` with its `--project-ref`; only direct operator mail that names no item leaves both out.

## Runtime Actions And Test Windows

A project's runtimes (`pb worker context`, `runtimes`) name, per action, who triggers it and the ref it releases in each repository it loads (`releases`); the commands live in the runtime's profile (`local_profile`), never here, and a project with none has no runtime actions ([runtime-actions](references/runtime-actions.md), Project Runtimes). Every action loads, per repository, the commit its ref names, never a working tree, and its result names each repository, ref and commit.
A runtime's reload, refresh or deploy is decided and proven by the coordinator and executed by the delegates it names ([coordinator](references/coordinator.md), The runtime is the operator's). A relay restart is host-local: the agents on that host agree, then the installer the route names for that host restarts it, else the coordinator on that host or its elected integrator (collaboration Rule 2). For a runtime action, ask the
coordinator, naming what you need live and the commit, pushed to the ref the action releases.
A client-source selection is one of these actions ([runtime-actions](references/runtime-actions.md), Client Source
Selection). A container-local patch is not an action this team has. Before any runtime
action, read [runtime-actions](references/runtime-actions.md), and for a test
window [test-window](references/test-window.md). A coordinator about to accept,
merge, route, reload or refresh follows the section of [coordinator](references/coordinator.md) that its act table names for that act, not the whole file. Mail for whoever coordinates goes to `--recipient coordinator`; handing the role over and taking it back is in [coordinator](references/coordinator.md) too. When you hold the coordinator role (home or acting), read its first section, What the coordinator is for, before anything else, once per role take: reread it only when `pb procedure verify` names `references/coordinator.md` in `changed_files`, never because of a compaction at the same installed revision. You work for the operator, speak to them unasked, and drive the team.

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
without it. A status is held to the same discipline, because a reader acts on it: [project report](references/project-report.md), "A Status Is Held To The Same Discipline".

## Review Foundations And Procedure Gaps

Before you decide or review identity, authority, storage, canonical
representation, ordering, paging, durability, retention or data flow, and when
you learn a procedural lesson or find a gap in this skill, read
[foundations and procedure gaps](references/foundations-and-procedure-gaps.md)
in full. A procedural lesson is filed against this package, as a revision or
as an item naming the rule and its evidence, before you settle the work you
learned it in, and the settle summary names which. A rule you find missing or
wrong never goes into private memory.

## Coordinate Research Progressively

The coordinator names one researcher for a question and tells the other workers
who owns it. The rest is in [coordinator](references/coordinator.md).
