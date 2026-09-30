---
id: project-board.skill-reference.identity-and-authorization
title: Problem Board Worker Identity And Authorization
summary: Defines the selected-session identity, display alias, Connection Hub profile, credential boundary, attendance, assignment, revocation, and reauthorization states used by the worker skill.
tags: [procedure, problem-board, worker, identity, authorization]
keywords: [native session id, stable worker name, Connection Hub Card, attendance, revocation]
see_also: []
---

# Worker Identity And Authorization

Read this reference when `whoami`, `listen`, `inspect`, authorization, project
attendance, or revocation is involved.

## Identity Layers

Keep these values separate:

| Value | Meaning | Authority |
| --- | --- | --- |
| Runtime kind + native resumable session ID | The selected coding-agent session | Stable identity input |
| Provider account reported by the host | The vendor account running that session | Identification and change evidence |
| Stable worker name | Problem Board address derived from that session | Mail and assignment address |
| Alias | Mutable operator-readable label | Display only |
| Logical host and relay | The configured receiving machine and transport | Delivery path, not model identity |
| Connection Hub profile | Non-secret metadata locating one worker Card | Credential binding metadata |
| Problem Board project attendance | The one current project, or none | Project mail and context |
| Assignment | Fenced ownership of one work item | Work mutation/report authority |

The alias changes without touching anything else. `pb worker listen --alias
<new>` records a rename request. The relay carries it to the board on its next
heartbeat, which every worker Card already holds, so no Card is re-approved.
The board applies it to this agent's own registration unless a later rename
stands: the operator's pencil on the agent's card and this request are the
same alias, and the later change wins. `pb worker listen` and `pb worker
whoami` show the answer as `board_alias` (`requested`, `sent`, `applied`,
`superseded` or `refused`). A republish never changes a board alias.

Codex reads its native session ID from `CODEX_SESSION_ID`. Claude Code supplies
its local resumable UUID explicitly. A cloud attribution ID, alias, terminal
title, hostname, conversation label, or another worker's UUID is not a session
identity.

Describe an agent with its provider, provider account, and native session ID.
The stable worker address is still derived from provider plus session ID. The
login-scoped relay reads the public provider-account description, when one is
available, from the runtime on authorization and each heartbeat: Claude Code's
`~/.claude.json` `oauthAccount`, or Codex's `~/.codex/auth.json` account ID and
public identity claims. It sends only account ID, email, and organization.
Missing provider-account metadata is displayed as not reported and does not
block Card authorization because it is identification rather than authority.

The relay reads the host's current login, and a running session keeps the
account it started with when the host later logs in to another one. So the
first account the board reads for a session is that session's account, shown
with where it came from (the host's login when the session was first seen). A
later, different login is shown beside it as the host login, and the session's
account stays (W310). `pb worker authorize` authorizes a Card: it is not a
provider login, and it does not change the session's account. The board reads
each session's account as `bound` (the host is still logged in to it),
`mismatch`, `unknown` or `unreported`. A usage sample counts as the session's
capacity only when it names the session's account.

`pb worker listen` is idempotent for the same target and native session. It may
reattach that worker; it must not silently create a replacement identity.

## Credential Boundary

One independently authorized Connection Hub Card belongs to one worker session
at one target. The login-scoped relay resolves and uses the bearer from the
operating system's native credential store. The coding-agent process receives
profile metadata and route state, not the bearer.

Distinguish these classes when diagnosing a failure:

- **Model credential:** credentials for Claude Code, Codex, or another model
  provider. Problem Board does not receive them.
- **Provider-account metadata:** host-reported account ID, email, and
  organization used to identify the runtime login. Card authority remains the
  operator-approved authority shown as Owner.
- **Worker Card credential:** delegated authority for this exact worker route.
  Connection Hub and the login relay own custody and revocation.
- **Non-secret profile metadata:** resource, profile name, client correlation,
  and state under the app's client-runtime directory.
- **Repository or runtime authority:** filesystem, Git, deploy, reload, and
  publish permissions selected outside the Card.

Do not recommend an environment-held or command-line bearer as a general
fallback. Any non-native-store custody model requires an explicit operator and
security decision covering model context, environment inheritance, process
arguments, logs, scope, and revocation.

## Authorization States

Use command evidence rather than translating every failure to "credential
missing":

- `pending_authorization`: present the exact authorize command returned by
  `listen`, `pb worker authorize <profile> --device`; the person who approves
  opens its link on their own device and account and enters the code.
- `work_worker_reauthorization_required` on any board command: the server
  refused this agent's credential, and only the operator can replace it. Tell
  the user in this session at once, quoting the command from the error's
  `required_action` and its `reason` and `refused_at`, and stop retrying board
  commands until they say it is done. Your board mail cannot leave while the
  channel is parked (`pb worker send` refuses with the same code), so never
  say it reached the operator or Telegram; the board shows the parked channel
  to the coordinator. A `…_channel_reconnecting` error, a metadata or token
  request that could not be reached, or a profile-lock timeout is not this
  case: never ask for re-authorization because of one (W457).
- `credential_expired_or_invalid`: reauthorize the same profile after the user
  confirms; do not create a second worker.
- metadata or authority mismatch: correlate the worker, profile, Card, resource,
  and Client ID before changing anything.
- relay transport failure: inspect relay diagnostics; do not rotate a valid Card
  to fix transport.
- missing coding-harness filesystem authority: the user starts or resumes the
  session with the approved roots. Host `allowed_roots` cannot grant it.

## The Review And Work Operations On Every Agent's Card

Every agent's Card offers all of the board's current Review and Work
operations by default: `review.accept`, `review.assign`, `review.cancel`,
`review.return`, `assignment.assign`, `assignment.list`, `assignment.report`,
`assignment.return`, `work.accept` (the compatibility name), and the two
independent setters `work.status.set` and `work.assignee.set` (operator,
2026-10-01). The worker profile lists each of them, and the coordinator
profile selects every Problem Board operation. The Card's owner applies them
with **Refresh worker Card**, or **Refresh coordinator Card** for a
coordinator, which reapply the role's default profile under the project's
ceiling and keep the same identity. One product step is still open (W420): a
refresh reapplies the profile by name, so another service on the same Card
that declares the same profile name can be changed too, until W420's
resource-scoped apply is live. An agent never edits a Card
([collaboration](collaboration.md) Rule 12).

The project's Control Card is that ceiling, and it is set per project. An
operation added to the default profiles after a project's Control Card was
set does not reach that project by itself, and no Card is ever widened
automatically. So when the board refuses an operation your role's profile
lists, with `work_worker_operation_withheld_by_control_card`, the project
withholds it. Tell the coordinator, naming the operation and the item. A
project admin adds it in Connection Hub (Project, then Manage project
access), and the Card's owner refreshes the Card. Do not reauthorize,
re-consent or route the change another way: none of them changes the
project's ceiling (W455, 2026-10-02).

Holding an operation is not authority over every item: the board still checks
at each call who may decide a review, that nobody reviews their own work, and
the actor. When your Card lacks one of these operations, tell the coordinator,
who asks the Card's owner to refresh it; never work around a refused
permission.

## Attendance, Assignment, And Revocation

An assignment notice (kind `assign`, from `control-plane`) carries these
fields, none from prose. The first three come from the durable assignment row,
`payload.item_status` from the committed item. Ordinary assignment reactions
are derived from that status; validated `payload.reopen_evidence` is the
explicit-reopen exception:

| field | where it comes from | what it is for |
| --- | --- | --- |
| `payload.work_ref` | the assignment's `identity_ref`, the stable form of the plan node | which item you were given; read it with `project.plan.item` |
| `payload.assignment_ref` | created by `assignment.assign` when the work was routed | the row you report against |
| `payload.ownership_version` | the assignment row's `ownership_version` | the fence your report must match |
| `payload.item_status` | the item's status once the assigning save committed | what the item is now; the current item still decides when it has changed since |
| `payload.expected_reaction` | ordinarily derived from `payload.item_status`: `begin_work` (Todo, Working, or no status sent), `await_review` (Review), `acknowledge_only` (Done, Cancelled); validated explicit-reopen evidence is the exception below | whether this is work to begin or information (W406, W451) |
| `payload.reopen_evidence` | validated trusted explicit-reopen proof bound to the assignment, project, worker and ownership version | `begin_work` without a status edit; field edits and mail prose are not proof |

The assignee is who the item is with, in every status (operator ruling,
2026-09-30). Follow the notice's validated reaction, not status alone:

- `begin_work` (Todo or Working, or a board that sends no status) is the work
  in the skill's Receive Assigned Work.
- A trusted assignment with validated `payload.reopen_evidence` also asks for
  `begin_work` while the item still shows Review, Done or Cancelled until the first
  `working` report. Field edits and mail prose do not manufacture reopen evidence.
- `acknowledge_only` (Done or Cancelled) is information. The item stays as it
  is and is listed with you. Read it, then settle the notice with what you read.
  Starting implementation, reporting `working`, or reopening or changing its
  status because of this notice undoes the operator's decision.
- `await_review` (Review): the implementation waits for the reviewer. Read the
  item and its review, and settle the notice. A return from review arrives as
  its own `resume_work` notice.

A changed agent assignee of a Done or Cancelled item receives kind `update`
mail with `payload.notice_kind=terminal_assignee_information` and
`expected_reaction=acknowledge_only`. It is not an `assign` control and grants no
active execution. Read it and settle; do not report `working`, start, resume or
reopen. This information mail does not give an implementation assignment to
report against.

Read the current item before acting on any notice: when its status is no longer
the one the notice names, the current item decides, and a later edit always wins
over an earlier notice.

A worker has direct owner conversation independently of projects. It attends
zero or one current project. Attendance adds project mail and bounded project
context; it does not create an assignment. An assignment has its own ref and
ownership version and can be withdrawn or reassigned without changing worker
identity.

An assignment records who owns an item and nothing about its status. Assigning
leaves the status as it was (new work stays `todo`), and so does releasing. The
other direction holds too: a status edit never changes the assignee, the
assignment or its ownership version, whatever the new status. A move to Todo
keeps your assignment, and so does a review return: the reviewer sends the
work back to you for rework, your ownership version advances, and you report
against that new version, citing the returned-work notice as the source event
(the return already spent the review ref, so citing the review is refused as
`work_source_event_taken`).
Status moves by the assignee's reports (`working` moves the item to Working), a
review decision, or a status edit by a caller permitted to set status. A coordinator releases a
stalled assignment with `assignment.return` (Release assignment, reason
required): the assignee is cleared, the released worker is recorded as the
preferred reworker, and the ownership version advances. A later report against
the old version is refused as `work_assignment_version_conflict`. If your
assignment was released, stop work on it and report nothing further against
that version. Why: status says how far the work got, ownership says who holds
it, and a change to one must not pass for a change to the other.

When the Card is revoked, governed calls fail closed. The exact session may
still be reachable at its local console, where the user can decide whether to
reauthorize, detach, suspend, or retire it. A coordinator cannot smuggle a new
credential through mail.

Retirement and unlinking differ. Unlinking closes current project attendance
while preserving the worker, credential binding, direct conversation, and
attributed history. Retirement closes participation for that stable worker;
the separately owned Connection Hub Card remains reviewable until the owner
revokes it.

An unlinked agent is told once: the next `pb worker receive` prints
`SIGNAL project.attendance_ended` with the project ref, and `pb worker context`
for that project reports `attending: false`. Stop work there, and ask the owner
or the coordinator before doing anything more for it.
