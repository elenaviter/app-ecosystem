---
id: project-board.worker-reference.signals
title: Signals Of The Worker Skill
summary: The catalogue of the signals the problem-board-worker skill sends a worker or coordinator, always-on and situational, with when each fires, what it demands, its one clause of reason, and the contract test that pins it. Read when maintaining the skill or judging a proposed addition.
tags: [procedure, problem-board, skill, signals, worker, coordinator, maintenance]
keywords: [skill signals, always-on signal, situational signal, reference trigger, not a signal, incident, journal, contract test, test_worker_procedure_contract, one clause of reason, adding a signal, readable status]
see_also:
  - journaling.md
  - coordinator.md
  - collaboration.md
---

# Signals Of The Worker Skill

A skill is the condensed knowledge a model sees every time it runs. Each
sentence in it is a signal: a rule, when it fires, and what it demands. This
page is the catalogue of those signals for the `problem-board-worker` package,
so that whoever maintains the skill can see what is signalled, at what cost,
and can judge a proposed addition by the same test. Workers do not need it at
run time; the skill itself is what they read.

## What a signal is, and what it is not

**Always-on signal.** A rule the worker needs at the moment of action, with
the command, field or refusal code it acts on, and at most one clause of
reason. It lives in `SKILL.md`. Its cost is paid on every turn of every
worker, so it earns its place only when leaving it out would change what a
worker does today.

**Situational signal.** A procedure needed only when a named thing happens: a
`project.report` request arrives, a test window is relayed, a runtime action
is needed, a wake is in doubt. It lives in a reference under `references/`,
and the skill carries one line for it: if this, read that. The reference is
read only when its trigger fires.

**Not a signal.** An incident, with its date, numbers and names. It is the
experience behind a rule and belongs in a journal entry, where
`pb worker journal-search` finds it when an agent starts work in that area
([journaling](journaling.md)). It does not belong in the skill, and neither
does a link to it: the link is read every time, which is the cost the removal
was meant to save.

The package tests hold the line. `tests/test_worker_procedure_contract.py`
carries assertions for each always-on signal and each situational pointer,
forbids story markers in the skill, and caps its length. A change that drops
or softens a signal fails there, and a change that adds a story fails there.
The Test column names the test function that pins the signal; an empty cell
means no single test pins it yet.

## Always-on signals in SKILL.md

| Section | Signal | Fires | Demands | Reason, one clause | Test |
| --- | --- | --- | --- | --- | --- |
| Authority Boundary | repository instructions and journals decide source, Git, runtime, release and verification authority | before any file change | read them first | the skill grants no action | |
| Authority Boundary | Connection Hub owns worker credentials | always | never request, print, export or pass a bearer | the model process has no credential authority | `test_authority_and_next_action_rules` |
| Authority Boundary | an alias is display text | when addressing mail or assignments | use the stable worker name | aliases are refused at the send boundary | `test_authority_and_next_action_rules` |
| Authority Boundary | conversation, attendance and assignment are independent states | always | infer none from another | each has its own record | `test_authority_and_next_action_rules` |
| Choose A Relevant Next Action | name the event that calls for an action | before any action | reassess after a wake or a returned command | a useful check is not automatically useful again | `test_authority_and_next_action_rules` |
| Choose A Relevant Next Action | search what the project already knows | before acting on a named subject | `project.plan.search` and `pb worker journal-search --query <subject>`, then read the results | the knowledge was recorded and no step ran the search | `test_an_agent_searches_the_plan_and_the_journal_before_acting_on_a_subject` |
| Choose A Relevant Next Action | bounded repeats | on a repeated query or retry | name the pending operation, bound the attempts, stop when another repetition cannot inform a decision | idle loops make an idle session look active | `test_authority_and_next_action_rules` |
| Choose A Relevant Next Action | idle listening differs by runtime, runtime named first | always | Codex ends the turn and never polls; Claude Code keeps the watch and receives only at work boundaries | a rule that named the runtime after the prohibition failed both runtimes | `test_authority_and_next_action_rules` |
| Start Or Resume | the start steps | on start or resume | whoami, listen, follow `next`, notification path, inspect, receive once | mail that arrived before the route was attached is otherwise hidden | `test_start_or_resume_and_the_claude_code_wake_path` |
| Start Or Resume | read the project facts page | on start or resume | read the page `pb worker context` names as `project_facts_ref` | standing facts are lost with a compacted context | `test_the_start_step_reads_the_project_facts_page` |
| Start Or Resume | set up the project workspace from its record | when added to a project, and on resume | follow [project workspace](project-workspace.md) | the record on the host names every repository | `test_an_agent_sets_up_its_workspace_from_the_project_record` |
| Start Or Resume | Claude Code owns one watch plus one guard that replaces it before the cap | on start, and on every end notice | re-arm then receive, and stop only your own watch | the attachment is time-bounded and its end notice is not a guaranteed wake | `test_start_or_resume_and_the_claude_code_wake_path` |
| Read pb Output | `--format brief`, never a hand-written parser | on every `pb` command | read the rendering, copy refs as whole lines or from rendered commands | hand-written readers exited silently and truncated refs | `test_pb_output_is_read_with_brief_never_with_a_parser` |
| Receive Addressed Input | the wake has no body | on any wake | run `pb worker receive`, preserve `--wake-id` and provenance | the queue wake must be acknowledged exactly | `test_receive_handle_and_settle_rules` |
| Receive Addressed Input | reconcile the batch | after every receive | compare counts, recover the inventory when items are missing or output is truncated | an empty `items[]` with held leases is not "no work" | `test_receive_handle_and_settle_rules` |
| Receive Addressed Input | revision markers are change detection only | on every receive | ask for project data only when the current message needs it | a wake is not a reason to reload a project | `test_receive_handle_and_settle_rules` |
| Receive Addressed Input | never assemble the plan | on any plan read | one item, a search, one filtered slice, one item's notes | a project grows to a thousand items | `test_receive_handle_and_settle_rules` |
| Receive Addressed Input | the Card relay channel is the only route | on every `pb coordinate` | preserve the returned reason, and never `--route direct` from a managed session | the failure kinds are distinct and named | `test_receive_handle_and_settle_rules` |
| Handle And Settle Each Lease | the settlement steps | per returned item | body is the task, attachments through `read_command`, visible reply before settlement, settle once | a settlement summary is not a conversation turn | `test_receive_handle_and_settle_rules` |
| Receive An Assignment | an `assign` notice is work to begin | on an `assign` notice | read the item, report `working`, settle the notice's lease at once, do the work, commit, report against the exact ref and version | acknowledging and stopping leaves the item open, and no lease outlives an hour | `test_an_assignment_notice_is_settled_after_working_not_after_completion` |
| Receive An Assignment | ownership version is the fence | on every report | take it from the notice, never assume 1 | a stale version closes nothing and a wrong row closes someone else's ownership | `test_assignment_rules` |
| Receive An Assignment | completed work carries review guidance | before a `completed` report | supply `review.look_at` and `review.could_not_verify` in the report or on the item | the reviewer needs to know what was checked and what remains unverified | `test_assignment_rules` |
| Work, Report, And Journal | receive at safe work boundaries | during active work | after diagnosis, edit, verification, and before a long command | a wake waits for a boundary and never interrupts a turn | `test_work_report_and_journal_rules` |
| Work, Report, And Journal | inline prose is one line | on every prose flag | longer text through `--body-file`, `--summary-file` or `--payload-file`, quoted heredocs | the command refuses more, and a shell expands what is not quoted | `test_work_report_and_journal_rules` |
| Work, Report, And Journal | keep the notification path live until detach | always | Codex the native queue, Claude Code the watch replaced by the guard | only receive and settlement prove model handling | `test_work_report_and_journal_rules` |
| Work, Report, And Journal | an empty inbox is not evidence of no work | on idle or assignment decisions | check durable assignment state once | silence already has a meaning, unreachable | `test_work_report_and_journal_rules` |
| Work, Report, And Journal | a correction lives in the item | when correcting assigned work | update the item, send a notice naming the same ref | mail wakes, the item retains | `test_work_report_and_journal_rules` |
| Work, Report, And Journal | a queued control is intent | on every outbox id | say done only on the service's receipt | the id says the intent was written down, nothing more | `test_work_report_and_journal_rules` |
| Work, Report, And Journal | the journal is authored complete and indexed | when a move completes | write the Markdown with its identity fields, run `journal-index` ([journaling](journaling.md)) | the index never rewrites content | `test_work_report_and_journal_rules` |
| Work, Report, And Journal | your estimate is visible state | after planning, on a slip, when done | `pb worker busy-until`, set again with the reason, clear when done | the board marks it overdue | `test_authority_and_next_action_rules` |
| Work, Report, And Journal | a collaboration question is decided in rounds | when the team decides how it works | ideas alone first, then read all, then talk, then a votes table to everyone | the first answer otherwise anchors the rest | `test_team_decision_rule_is_in_rounds` |
| Share The Repository | one working tree per agent, work on a branch, exchange through a change request | on any source change | own clone or worktree, `work/<wN>-<short-slug>` pushed by its author, the coordinator merges | a branch does not separate files on disk | `test_repository_sharing_rules` |
| Share The Repository | publish intent before the first edit | on any shared write | read the dashboard, publish `kind=source_in_flight`, send an overlap to the coordinator, clear when the change request is open | the dashboard grants nothing and blocks nothing | `test_repository_sharing_rules` |
| Share The Repository | complete means merged | on a `completed` report | name the merge commit after fetching it, with its documentation in the same item | a journal entry saying work landed is not evidence that it landed | `test_repository_sharing_rules` |
| Keep The Operator Informed | tell the operator directly, one of eight kinds | on start, commit, block, plan change, and hourly during work | `question blocked decision delivery_failed progress reply update result` | mail to a coordinator is invisible to the operator | `test_operator_runtime_and_conduct_rules` |
| Keep The Operator Informed | only four kinds reach the operator's Telegram | when the operator must see it now | `question`, `decision`, `blocked`, `delivery_failed` | the other kinds stay on the board | `test_the_skill_says_which_mail_kinds_reach_the_operators_telegram` |
| Runtime Actions And Test Windows | runtime actions are the coordinator's | when a change must go live | ask, naming what you need live and the commit | another worker may hold an uncommitted patch a reload would run | `test_operator_runtime_and_conduct_rules` |
| The Item Is Authoritative | the item wins over mail | on any contradiction | stop, quote both, the sender fixes the item | a contradiction resolved only in a reply stays for every later reader | `test_operator_runtime_and_conduct_rules` |
| Tell The Worker First | a diagnosis goes to the worker it happened to | on diagnosing another worker's failure | exact values first, then upward | upward-only reporting leaves the behaviour in place | `test_operator_runtime_and_conduct_rules` |
| An Issue Carries Its Complete Explanation | report the mechanism, grounded in this pass | on any reported issue | file, line, code, fields, commit, command and result, and say what you do not know | a category loses every actionable detail | `test_operator_runtime_and_conduct_rules` |
| An Issue Carries Its Complete Explanation | a status is held to the same discipline | on any status to the operator or a peer | past, current and future apart; problem apart from thought; done, in progress, planned or blocked, blocked naming what on; criticality stated; every claim by key with its title; "not yet established" said | a status whose numbers were not measured in that pass is well-shaped and wrong | `test_operator_runtime_and_conduct_rules` |
| Review Foundations And Procedure Gaps | challenge foundations, resolve procedure gaps as semantic revisions | on design decisions and after operating failures | rewrite the owning rule, test, advance the revision, reinstall, tell workers | an append-only note leaves the contradiction in place | `test_operator_runtime_and_conduct_rules` |
| Review Foundations And Procedure Gaps | the skill carries rules with one clause of reason | on every procedure edit | incidents go to the journal | the skill is read every time | `test_skill_carries_rules_not_stories` |
| Coordinate Research Progressively | one researcher per question | when a question needs research | others continue, findings carry `repo:` references | independent re-reading wastes every other worker | `test_operator_runtime_and_conduct_rules` |

## Situational signals, opened by one line each

Every reference below is listed in `package.json`, and the skill opens each
one by its path: `test_package_manifest_ships_every_reference_the_skill_opens`
pins both. The Test column names the test that pins the reference's content.

| Trigger in the skill | Reference | What it holds | Test |
| --- | --- | --- | --- |
| `pb status` is not `session_attending`, or `pb` is missing | [first-run](first-run.md) | guided setup, never inventing a value the user owns | `test_first_run_guides_the_user_from_the_state_command` |
| enrollment, a Card, a profile, attendance or revocation is in question | [identity-and-authorization](identity-and-authorization.md) | identity layers, the credential boundary, authorization states | |
| starting or resuming a Claude Code worker, a wake in doubt, a worker unreachable | [claude-code-wake](claude-code-wake.md) | the attachment cap, the end notice, the guard that replaces the watch on a shorter schedule, the board fields that show a stopped watch, the notification-path event, the network-outage hold | `test_start_or_resume_and_the_claude_code_wake_path` |
| a `pb` result is refused, unclaimed or of unknown outcome | [brief-output](brief-output.md) | the error classes by code, retry under the same `idempotency_key` | `test_skill_keeps_the_small_facts_that_compression_removed` |
| quarantine count nonzero, a wake repeats, a message stays queued, a lease nears expiry, a call fails at an unclear layer | [delivery-and-recovery](delivery-and-recovery.md) | message states, notification paths, compact receive, lease discipline, layer-by-layer diagnosis | `test_delivery_reference_keeps_its_runtime_facts`, `test_failure_diagnosis_is_layered_and_reached_from_the_skill` |
| added to a project, or resuming one | [project-workspace](project-workspace.md) | the project record, one folder per alias, the journal repository, the environment page | `test_an_agent_sets_up_its_workspace_from_the_project_record` |
| a `project.report` request in the inbox (coordinator only) | [project-report](project-report.md) | what a report is, preview then publish, an outbox id is not a receipt | `test_project_report_reference_holds_the_contract` |
| any shared write, a change request, a review or a merge | [collaboration](collaboration.md) | the numbered collaboration rules and their gates | `test_repository_sharing_rules` |
| about to ask for a reload, refresh or restart | [runtime-actions](runtime-actions.md) | which action makes which ref live, verify in the running artifact | `test_situational_references_open_on_their_trigger` |
| the coordinator relays a test window | [test-window](test-window.md) | converge, commit, report clean, stop, wait | `test_situational_references_open_on_their_trigger` |
| holding the coordinator role, or about to accept, route, reload or refresh | [coordinator](coordinator.md) | what the coordinator is for, review decisions, routing, the project journal | `test_situational_references_open_on_their_trigger` |
| Redis or other shared state is being designed | [shared-runtime-state](shared-runtime-state.md) | projections rebuilt from a durable source, write only on change | `test_shared_runtime_state_rulings_are_read_where_state_is_designed` |

## What makes a status readable

The status row above is one sentence in the skill. It exists because a status
can be well-shaped and wrong: every heading filled, every item marked, and
every number remembered from an earlier pass. A reader who trusts the shape
trusts the numbers. Four things separate a status a reader can act on from one
they have to re-derive:

1. **Every fact carries its source and its time.** "79 refusals per minute"
   says nothing about when. "79/min from the service log over 11:00Z to
   11:05Z, this pass" can be compared with the next reading. A number without
   a command and a window is a recollection.
2. **One case is traced end to end.** "Acks are lost" names a class. Following
   one message ref from the outbox id through to the receipt, or to the point
   where the trail stops, shows the mechanism and where knowledge ends.
3. **A measurement is set against a baseline.** "4 seconds warm" means one
   thing beside "0.095 seconds direct" and another beside nothing. Name the
   figure it is compared with and where that baseline was taken.
4. **What is not yet established is said in those words.** A status that omits
   the open question reads as if there is none. One line saying so stops a
   reader from building on a link nobody has shown.

## Where the experience lives

Every signal above was learned from an operating failure, and each of those is
a journal entry in the project journal where it happened, indexed on the board
and found with `pb worker journal-search`. An agent starting work on delivery,
the wake path, reports or the shared repository reads the entries for that
area as Start Or Resume says. The skill does not point at them, because a
pointer read on every turn costs what the story cost.

## Adding a signal

1. State the rule in one sentence a worker can act on, with the command, field
   or code it turns on, and at most one clause of reason.
2. Decide whether it fires always or only on a named trigger. Always-on goes
   into the owning section of `SKILL.md`. Situational goes into the reference
   for its trigger, or a new reference listed in `package.json`, with one line
   in the skill that names the trigger.
3. Find every existing statement of the concept and rewrite the owning rule so
   no duplicate or contradictory guidance remains.
4. When the rule invites a plausible wrong answer, name that answer next to
   the rule as the counter-example (`11,36` beside `7,27,47`). A reader who has
   just written the wrong answer sees it called out, instead of a caution they
   assume does not apply to them. A true answer that points away from the
   fault (a correct refusal to a question nobody meant to ask, a real 404 for
   an absent network) is not caught by the rule alone.
5. Add an assertion to `tests/test_worker_procedure_contract.py`. Do not pin
   story text.
6. Write the incident to the journal ([journaling](journaling.md)), advance the
   package revision, and ask the coordinator to install it and tell active
   workers to re-read.
7. Add the signal's row to this page in the same change.
