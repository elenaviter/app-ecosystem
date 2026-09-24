---
id: applications.playground.problem-board.skill-reference.identity-and-authorization
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
When the account ID changes for the same session, Problem Board preserves the
worker address and reports the change to the operator and worker.

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
  `listen`; the user completes consent.
- `credential_expired_or_invalid`: reauthorize the same profile after the user
  confirms; do not create a second worker.
- metadata or authority mismatch: correlate the worker, profile, Card, resource,
  and Client ID before changing anything.
- relay transport failure: inspect relay diagnostics; do not rotate a valid Card
  to fix transport.
- missing coding-harness filesystem authority: the user starts or resumes the
  session with the approved roots. Host `allowed_roots` cannot grant it.

## Attendance, Assignment, And Revocation

An assignment notice (kind `assign`, from `control-plane`) carries these
fields, every one from the durable assignment row and none from prose:

| field | where it comes from | what it is for |
| --- | --- | --- |
| `payload.work_ref` | the assignment's `identity_ref`, the stable form of the plan node | which item you were given; read it with `project.plan.item` |
| `payload.assignment_ref` | created by `assignment.assign` when the work was routed | the row you report against |
| `payload.ownership_version` | the assignment row's `ownership_version` | the fence your report must match |
| `payload.expected_reaction` | constant `begin_work` | says this is work, not information |

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
