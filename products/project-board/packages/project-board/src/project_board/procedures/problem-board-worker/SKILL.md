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
When you start work on a named subject (a host, a feature, an item), search once for what the project already knows about it, not again at every step of the same task: `project.plan.search` for the plan and `pb worker journal-search --query <subject>` for the journal of the project you attend (add `--project-ref` to name the project explicitly), then read what they return. Before filing a plan item, follow [collaboration](references/collaboration.md) Rule 14: search first, note small things on an open item, and tell the coordinator what you filed.

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
1. Read the repository instructions of the folder you are in. What `pb worker context` names is read after you attend a project (step 7): the command needs enrollment and attendance first.
   An agent attends one project at a time (a link to another is refused until it is unlinked).
2. Identify this exact runtime session: `pb worker whoami`.
3. Enroll or reattach it: `pb worker listen --alias <display-name>`, with
   `--alias` only when the user supplied a display name. The person's side is [enroll an agent](repo:app-ecosystem/products/project-board/packages/project-board/src/project_board/procedures/enroll-an-agent.md).
4. Follow `next`; present its exact `pb worker authorize <profile> --device`. Do not reconstruct a profile name, and never drop `--device`: the person who approves the Card owns it and may not be the one signed in to a browser on this machine (operator, 2026-09-26). Tell them to open the printed link on their own device, in their own account, and enter the code. Device login has no callback or tunnel fallback (operator, 2026-09-30): when it fails, report its named refusal code to the operator and change nothing; the handoff exposes only public URL/code and the credential goes to the native store. Claude Code: start the step-5 watch before this step, so the approval arrives as its `control_plane.connected` event (a Codex session is woken by its relay). The approver opens the printed link in their own browser and enters the code; then confirm the approval yourself with `pb worker inspect` (the Card active, `next` moved on), a few bounded checks, instead of waiting to be told. Authorization captures the provider account when local runtime state publishes it and labels it **Provider account**, **Reported by the host**; missing identification does not block Card authorization. [Identity and authorization](references/identity-and-authorization.md) owns the account and Card-authority contract.
5. Establish the notification path returned for this runtime:

   - **Codex:** the persistent login relay owns the `codex-queue` subscription
     and invokes `codex queue --thread <this-native-session-id>`. Do not start
     `pb worker watch` as a wake mechanism. Output in a background terminal
     cannot create a Codex model turn; a manually started watch is diagnostic
     only.
   - **Claude Code:** start exactly one session-scoped notification attachment
     with the Monitor tool, `timeout_ms` 1800000, `<id>` this session's id:

     ```bash
     exec pb worker watch --runtime-kind claude-code --runtime-session-id <id> 2>&1
     ```

     Schedule one recurring guard prompt that replaces it on a schedule whose
     every interval, including the wrap of the hour, is shorter than its
     30-minute cap. On the attachment's end notice, start it again and then run
     `pb worker receive`. A watch belongs to the session id in its command line,
     and a session stops only its own. Read
     [claude-code-wake](references/claude-code-wake.md) for why a background
     shell is not the facility, the guard prompt, the board-side fields that
     show a stopped watch, and what a network outage does to both wake channels.
6. Confirm the session and route state:

   ```bash
   pb worker inspect
   ```

   Codex: the subscription adapter is `codex-queue` and the login relay is
   live. Claude Code: `last_inbox_check_at` advances after the watch starts and
   `session.inbox_check_state` reads `current`.
7. Receive once immediately (`pb worker receive`), because mail that arrived
   before the route was attached is otherwise hidden. Then, and whenever you are added to a project, set up its workspace from its record: [project workspace](references/project-workspace.md). Your workspace is the `workspace` `pb worker context` names (the host's root, one folder per agent), never the folder this session started in and never a path you choose: create it there and work from it. `pb worker connect-project` clones the project's repositories into it, makes this machine's deploy key for any it cannot reach yet, sets the commit identity and reports it (first-run, Part 2). When it names none, ask the operator for a host root. Then read the project files, before any work: project files are the project's shared, current knowledge, and every agent reads them in its own clone and follows them. `pb worker context` gives the three purpose files, Instructions (`project_instructions_ref`: what the project is, its rules and conventions, how work is done there), Facts (`project_facts_ref`: the decisions and rulings in force now) and Environment (`project_environment_ref`: machines, runtimes, how to test and deploy), each with its path in your clone and whether it is there, and the further files with their one-line descriptions: read a further file when its description fits the task at hand. When `pb worker receive` names a changed project file (`project.files.changed`), reread it before you go on ([project workspace](references/project-workspace.md), "Project files"). An empty instructions ref means the project has none yet: ask the coordinator. Then read the newest journal entries and those for the work being resumed (`pb worker journal-search`). For the subject of the task, search the plan and the journal (Choose A Relevant Next Action).

Read [identity and authorization](references/identity-and-authorization.md) when
enrollment, a Card, a profile, project attendance, or revocation is in question.

## Keep The Critical Requirements As Memories

A few requirements must survive a compaction or a restart. When you load a revision of this skill, save each one below as one entry in your runtime's persistent memory, tagged `source: problem-board-worker <installed revision>`, and replace the whole set when the installed revision changes. Do not save your own versions of Problem Board workflow rules: a rule you find missing or wrong goes to the coordinator as a procedure change (collaboration Rule 14), never into private memory, because a memory you wrote yourself cannot show that you follow this procedure (operator, 2026-10-03).

1. Anything that waits on the operator is a work item assigned to them, with the exact steps and the expected result, plus a `decision` or `question` message naming it; never only in a terminal ([collaboration](references/collaboration.md) Rule 11).
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
Read-heavy `worker context`, `worker journal-search`, `source status`, `project.plan.search`, `project.plan.item`, and `assignment.list` instead print bounded decision summaries. Every displayed ref, id, key, cursor, commit and path stays whole.
When the exact omitted field or full prose is required, rerun that same narrow command with `--format json` and read its envelope directly; do not widen the query or write a parser. `pb render --file <path>` renders saved output the same way.

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
(creation time, attempts, host, relay, process) in any duplicate-wake report. **A wake asks for receive and handling, not for reloading instructions.** A new session loads this skill completely once. An existing session loads it completely again only when its installed revision actually changed through a regular upgrade, that is when `pb procedure verify` names a revision other than the one you loaded. A coordinator notice alone, new mail, a new turn and a compaction are not that. Keep the revision you loaded (the `revision=` of the managed-package marker under this file's front matter, which `pb procedure verify` reports as `installed_revision`) in your notes and in any compaction summary or handoff: it is the baseline `pb procedure verify` is compared with. After a compaction, before an act whose rule you no longer hold, read that act's section of this skill or its reference, not the whole package. Editing this package reads its source files, which is authoring, not loading. A reference is read when its trigger fires or the task needs it. Instructions still in your context stay valid across wakes, while task evidence does not and is read fresh as Choose A Relevant Next Action says. Why: rereading an unchanged skill fills the context it is meant to save.

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
   `assignment_ref` and `ownership_version` from the notice; `--reviewer` names the qualified teammate who took the review after you asked one or two who are available now; only when none is available, name the acting coordinator's stable worker name, so the review lands on it and not on you (operator, 2026-10-01 and 2026-10-03; collaboration Rules 6 and 16).

`working` and `blocked` are progress reports; `completed` and `refused` are
terminal. State plus `source_event_ref` identifies one immutable report: an
unchanged retry replays its receipt, and changed content under the same
identity is refused as `work_event_idempotency_conflict`. A source event is
spent by the first report that cites it, so each report cites the event that
prompted it: the assignment notice for the first, and for each later one,
completion included, the later mail, item revision or result event, never
that notice again. Progress to completion needs no reissued assignment. An
accepted terminal report is final for that ownership version.

A review is begun the same way: read the item and the exact head the notice names, publish that you started (`pb worker busy-until` with the review as its note), settle, review, and decide as [collaboration](references/collaboration.md) Rule 6 says. When you cannot start either kind, say so at the first safe boundary: `blocked` (or your info line, for a review) naming the reason, the actor or event that clears it and the next decision time, and tell the coordinator. Silence is never a state.

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
  explicit project or message evidence; when it is absent, put it in your one consolidated clarification ([collaboration](references/collaboration.md) Rule 16).
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
- An empty inbox is not evidence that there is no work. At session start, on resume and before `pb worker idle`, read your responsibilities once: `pb coordinate assignment.list` for your implementation work, `project.plan.index` with `{"assignee": "<your stable name>"}` for every item assigned to you, reviews routed to you included (add `"status": "review"` to see only those), and `pb worker outbox-status` for each outbox id whose outcome you do not know. You are idle only when each is done, started, or deferred with its reason, clearing actor or event and next decision time. A read that fails leaves you unknown, not idle. Read it again once per native wake batch and, while you work, about every 30 minutes at the next safe boundary, never per command, per leased message or per guard prompt, and act on each item or ask: [collaboration](references/collaboration.md) Rule 6, "Reconcile your assignments".
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
- Journal what the work taught the project (why this and not that, failures and their mechanism, wrong assumptions, limits) only when the project declares role `journal`; where the work stands goes on the item, not in the journal; no role means no journal work. The role is repository agnostic and every change is item-scoped: [journaling](references/journaling.md). Each signal of this skill and the test that pins it: [signals](references/signals.md).
- Author the complete journal Markdown, front matter included, at the configured relative path in the current item's worktree. Lead with the lesson; keep the
  mechanism, the rejected alternatives and why, the operator's exact ruling and
  the evidence that proves it ([journaling](references/journaling.md)); no empty sections or generic tags. The front matter needs a unique
  `work:journal:<created-at>:<entry-id>:<semantic-name>` `entry_ref` (semantic
  name at most 64 characters of `a-z0-9-`, else `journal_entry_ref_invalid`)
  and this exact `project_ref`; `title`, `summary`, `keywords`, `see_also`,
  status and attribution make retrieval better. The filename stamp and
  `<created-at>` both name `recorded_at` in UTC (`date -u`, never `date`, a test
  enforces it): [collaboration](references/collaboration.md), finding ten.
- After the journal change request is merged, fetch and fast-forward the clean journal clone, then run `pb worker journal-index --project-ref ... --repository-journal-ref ...`; an unmerged entry correctly returns `journal_entry_not_found`. The command does not rewrite the file and returns its index, validation and receipt steps.
  After interruption, inspect with `pb worker journal-index-status --project-ref ... --operation-id ...`, then run `pb worker journal-index-resume --operation-id ...` for the first incomplete step. Status is observation only: it does not rebuild, enqueue, or repair. Do not rerun the original command to guess what happened.
  For a pre-ledger validation use `journal-index-status --project-ref ... --outbox-id ... --repository-journal-ref ...`; it distinguishes an accepted plan revision from an absent receipt.
  Search with `pb worker journal-search --query ...` (`--project-ref ...` to name the project explicitly); legacy files remain searchable under a path-derived identity and status names compatibility issues.
- Your estimate is visible state. After planning, `pb worker busy-until <UTC> --note <one line>`
  says until when you expect to finish and what you are on. Set it again with the reason when it
  slips. Clear it with `pb worker busy-until --clear` when the work is done. The board shows it and
  marks it overdue once the time has passed ([collaboration](references/collaboration.md), rule 6). What the operator told you that the team must know about you goes on your cards with `pb worker info write` (same rule, The info line), and so does a pause you choose.
- How the team collaborates is decided in rounds, ideas alone first, then read all, then talk, then a votes table to everyone (rule 7). Handoff is an ownership decision the coordinator takes (rule 8), what you publish is safe to publish (rule 9), a runtime window speaks one channel that survives it (rule 10), a shared name or field is settled in one exchange and crossed messages are decided by its owner with "do not reply" (rule 15), and every task has a living route on its item that names each actor's next step: you start at once with one consolidated clarification, arrange and if needed replace your reviewer, read availability before you wait on anyone and again before you read their silence, and raise an unavailable work owner to the coordinator, who hands it off (rule 16): all in [collaboration](references/collaboration.md).
- When assigned work transitions to no work remaining, say so once with `pb worker idle`. When
  this exact session stops participating, run `pb worker detach`.

## Share The Repository With The Other Workers

Several agents work on the same repositories at once. The rules that keep
them apart are the collaboration procedure, [collaboration](references/collaboration.md),
revised one rehearsal round at a time. What every worker does, from it:

- **One working tree per agent, registered.** Register every tree you create (`pb worker workspace --path ... [--kind review]`); a sweep removes finished, clean, fully pushed trees at session start, on idle and after a review decision ([project workspace](references/project-workspace.md), section 6). Develop in your own clone or `git worktree`.
  Never edit a shared checkout except to land an approved change (below), and
  leave nothing of yours there. Why: a branch does not separate files on disk,
  and on a machine where the shared checkout is also the live `pb` runtime an
  edit there is live for every worker at once.
- **Work on a branch, exchange through a change request.** Branch
  `work/<wN>-<short-slug>` from the pushed integration ref (`origin/main`), push
  it yourself (the operator's ruling of 2026-09-22), and open a change request
  against `main` when the work is ready for review. Commit each coherent piece
  as you finish it. Put the link on the item and in your report. The merger the item's route names (a permitted non-author), or the coordinator when none is named, merges after approval and pushes the integration ref, with no further acknowledgement.
  Deploying stays the operator's. A branch is closed by its merge, a later push is a
  new change request, and you delete your branch when it merges or you abandon it.
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
- **As reviewer or merger, use the detached exact-head review tree under
  `<workspace>/rv/` that [project workspace](references/project-workspace.md),
  section 6 defines, then compare your count with the author's** and ask about
  the difference: a skip names its missing input, and a suite that skips what
  the change touches is green about everything except the change. Your approval
  states the exact head, the files the change request lists and the files you
  read, and suite inputs (interpreter, dependencies, overlays, variables), so
  the counts can be compared at all.
- **A `completed` report submits the source for review** at an exact head and
  change request, and its could-not-verify names what is still to come (merge,
  activation). Approval, merge, activation and whole-item acceptance are
  separate milestones ([collaboration](references/collaboration.md) Rule 6). The merge milestone names the merge commit after you fetched and ran `git merge-base --is-ancestor <commit> origin/main`. The acceptor runs it on their own clone. Any
  claim that a change landed (a report, an item note, a journal lesson) names that merge commit after you fetched it, never the intention
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

`progress`, `update`, `reply` and `result` stay on the board; only `question`, `decision`,
`blocked`, `delivery_failed` reach their Telegram, and a `reply` that keeps the correlation of a message the operator sent from Telegram goes back there. A board send is in the operator's board inbox; say it reached Telegram only when its kind or correlation sends it there and its receipt says delivered. Ask for their input this way, never in a terminal prompt: anything that waits on the operator is first a work item assigned to them, saying exactly what to do and what to expect, and then a `decision` or `question` naming it
(collaboration Rule 11). Other kinds are refused with `work_mail_kind_invalid`. Mail about a plan item carries `--work-ref` with its `--project-ref`; only direct operator mail that names no item leaves both out.

## Runtime Actions And Test Windows

A project's runtimes (`pb worker context`, `runtimes`) name, per action, who triggers it and the ref it releases in each repository it loads (`releases`); the commands live in the runtime's profile (`local_profile`), never here, and a project with none has no runtime actions ([runtime-actions](references/runtime-actions.md), Project Runtimes). Every action loads, per repository, the commit its ref names, never a working tree, and its result names each repository, ref and commit.
A runtime's reload, refresh or deploy is decided and proven by the coordinator and executed by the delegates it names ([coordinator](references/coordinator.md), The runtime is the operator's). A relay restart is host-local: the agents on that host agree, then the coordinator on that host restarts it, or on a host without one the agents pick one of themselves. For a runtime action, ask the
coordinator, naming what you need live and the commit, pushed to the ref the action releases.
A client-source selection is one of these actions ([runtime-actions](references/runtime-actions.md), Client Source
Selection). A container-local patch is not an action this team has. Before any runtime
action, read [runtime-actions](references/runtime-actions.md), and for a test
window [test-window](references/test-window.md). A coordinator about to accept,
merge, route, reload or refresh follows [coordinator](references/coordinator.md). Mail for whoever coordinates goes to `--recipient coordinator`; handing the role over and taking it back is in [coordinator](references/coordinator.md) too. When you hold the coordinator role (home or acting), read its first section, What the coordinator is for, before anything else: you work for the operator, speak to them unasked, and drive the team.

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
contract, leave the revision to the merger, reinstall it, and tell active workers
the new revision: each loads it once, when `pb procedure verify` names it. With uncertain ownership or policy, create a work item with the
evidence and ask. A procedure update is a semantic revision of the affected
contract, never an append-only note, and it carries the rule with one clause of
reason. Incidents go to the journal, where search finds them when they are
needed, and not into this skill, which every session carries in its context.

## Coordinate Research Progressively

The coordinator names one researcher for a question and tells the other workers
who owns it. The rest is in [coordinator](references/coordinator.md).
