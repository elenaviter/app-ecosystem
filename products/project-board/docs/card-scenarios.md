---
id: project-board-card-scenarios
title: Card Scenarios That Must Work
summary: Every action a person or agent can take on a Card in a Problem Board project, with who may take it, the route it must take while Card transactions are on, and the expected result. Readiness is checked against this list, row by row, by one standing test.
tags:
  - project-board
  - cards
  - connection-hub
  - card-transactions
  - readiness
keywords:
  - My Card
  - Control Card
  - agent Card
  - invitation
  - managed-card-edit.v1
  - card_transactions_direct_write_refused
  - delegated_agent_grant_create
  - W730
see_also:
  - ./cards.md
  - ./flows.md
  - ../../../docs/connection-hub/package/card-transactions.md
---

# Card Scenarios That Must Work

This is the list a Card release is checked against. Each row is one thing a person or an agent can do to a
Card. It says who may do it, the screen request that carries it, the route it must take while Card
transactions are on, and the result the person must see. A row is ready only when the standing test (W730)
runs it on the production code path and gets that result. A row the test does not cover is not ready.

Why this exists. On 11 October 2026 the operator's Saves failed one button at a time (W705). Each fix
covered the button that had just failed, because no list of all Card actions existed. The operator, verbatim:
"do you have the whole list of sceanrios and flows that must work? written in form of docymentation in PB /
connection hub? if no you absolutely must have it, and check the readiness against this architecture docs".

## The rule every row follows

- Who edits which Card is set by [Cards And Who Edits Them](cards.md#the-rules):
  - a project admin edits every Card the project holds: the project Control Card (P), each person's Control
    Card (C), and each agent's project Card (A);
  - a member reads their own C and edits only their own My Card (My), within their C.
- While Card transactions are on (W578), a change to the authority of a project-managed Card is decided by
  the project. Authority means operations, grants, properties, account scope, state, credentials, or a shorter
  expiry.
  - The Hub never writes such a change directly.
  - Every screen request that makes such a change is forwarded to the project as a signed managed edit and
    committed through the Card version transaction.
  - A request the project refuses changes nothing and never falls back to a direct write.
- A direct write the rule permits on a managed Card changes no authority field. Examples: renewing to a later
  expiry, a label.
- A refusal names the reason and the Card. The screen keeps the draft.
- An operation no Card decides, for example a membership read such as `project.control.get`, is still shown on a
  person's Cards, as in effect and disabled, never hidden. The operator, 11 October 2026 about 13:58Z, verbatim: "Cards hide
  project.control.get since it's a membership read no Card decides << we still show this (simply disabled)" and
  "ie.. we must", and "because for other permisisons that also card does not decide we still show them". This supersedes
  the W560/W667 hiding of 2026-10-09; the same rule applies to every such operation.

## Card kinds

| Code | Card | Held by | Edited by |
|---|---|---|---|
| P | project Control Card | the project | a project admin |
| C | a person's Control Card in the project | the project, for that person | a project admin |
| My | a person's My Card in the project | the person | that person, within C |
| A | an agent's project Card | the agent's owner, linked to P | a project admin, or the agent's owner for its own selection |
| I | an invitation's pending Control Card | the project, until redeemed | a project admin |

## Scenarios

The Status column records evidence, not intent. `live PASS` and `live FAIL` cite the operator's own browser
attempt with its time. `unconfirmed` means no test has run it yet. Times are UTC, 11 October 2026.

### Reading

| ID | Actor | Card | Action | Screen request | Expected | Status |
|---|---|---|---|---|---|---|
| R1 | admin | P | Open "Manage project access" | PB `project.control.get`, then the Hub `project_control_card_get` | P opens; the button is enabled for an admin | live FAIL 13:44: button disabled, cause not yet known |
| R2 | member | own C | Open own person Card | Hub `project_person_control_get` | Reads C; editing is admin-only | live PASS 13:42:37 |
| R3 | member | own My | Open My Card | Hub `delegated_access_list` / Card get | Reads My within C | live PASS 13:46 |
| R5 | member | own C and My | See the operations no Card decides (membership reads such as `project.control.get`) | Hub person Card editor | Shown as in effect and disabled; never hidden | live FAIL 13:48: `project.control.get` hidden, while other undecided operations are shown disabled (inconsistent) |
| R4 | admin | A | Open an agent's Card | Hub `project_agent_card_get` | Reads A and its link to P | unconfirmed |

### Selection Saves (operations already offered on the Card)

| ID | Actor | Card | Action | Screen request | Expected | Status |
|---|---|---|---|---|---|---|
| S1 | admin | C | Tick or untick operations, Save | Hub `project_person_control_update`, forwarded as `person_control` | Committed through the project | live PASS 13:43:21 |
| S2 | admin | C | Save twice without reload | as S1, twice | Both commit; the revision goes up by 2 | live PASS 13:43:21 and 13:43:52 |
| S3 | member | own My | Untick a held operation, Save | Hub `delegated_access_update`, forwarded as `person_my` | Committed through the project | live PASS 13:46:33 (base Save) |
| S4 | member | own My | Tick a NEW operation, Save | base Save, then Hub `delegated_agent_grant_create` per added operation | All ticked operations committed through the project, with the approval policy the person chose | live FAIL 13:46: the add was refused `card_transactions_direct_write_refused` (W727) |
| S5 | member | another person's My | Save | `delegated_access_update` | Refused `card_edit_actor_role_denied`; nothing changes | unconfirmed |
| S6 | admin | A | Tick or untick held tools, Save | Hub `project_agent_card_update`, forwarded as `agent_card` | Committed through the project | unconfirmed |
| S7 | admin or owner | A | Tick a NEW tool, Save | base Save, then `delegated_agent_grant_create` | As S4 | unconfirmed; same code path as S4, expected FAIL (W727) |
| S8 | admin | P | Narrow or widen P, Save | Hub `project_control_card_update` / `delegated_access_update`, forwarded as `project_control` | Committed through the project; agents capped by the new P | unconfirmed |
| S9 | admin | I | Edit a pending invitation's selection | Hub `project_person_control_update` with `invitation_ref`, forwarded as `invitation_control` | Committed through the project | unconfirmed |
| S10 | member | own C | Try to edit own C | `project_person_control_update` | Refused (admin only); the screen says why | unconfirmed |

### Accept update (catalog drift)

| ID | Actor | Card | Action | Screen request | Expected | Status |
|---|---|---|---|---|---|---|
| U1 | admin | C | Accept update with a selection change | S1 plus `accepted_operations` | Both are recorded; on Edit the update is shown as accepted | live: the 13:43 Saves committed; whether they carried an acceptance is not yet recorded |
| U2 | admin | C | Accept update alone | S1 with an empty selection | Committed; the acceptance is recorded | unconfirmed live (PB test PASS) |
| U3 | member | own My | Accept update | S3 plus `accepted_operations` | As U1 | unconfirmed |
| U4 | admin | A | Accept update alone | S6 plus `accepted_operations` | As U2 | unconfirmed live (PB matrix PASS) |
| U5 | admin | P | Accept update alone | S8 plus `accepted_operations` | As U2 | unconfirmed live (PB matrix PASS) |
| U6 | any | any | Accept under a catalog that has since moved | `expected_catalog_version` | Refused `stage_catalog_moved`; nothing changes | unconfirmed live (PB test PASS) |

### Policies, renewal, label

| ID | Actor | Card | Action | Screen request | Expected | Status |
|---|---|---|---|---|---|---|
| X1 | holder | My / A | Change a held operation's approval policy (always, once) | Hub `delegated_invocation_policy_set` | Saved; this is stored in the invocation-policy store, not on the Card | unconfirmed (code reading: not a Card write) |
| X2 | holder | managed Card | Renew to a later expiry | Hub `delegated_access_renew` | Saved; the rule permits it (expiry only forward) | unconfirmed |
| X3 | holder | managed Card | Rename (label only) | `delegated_access_update` with a label | Saved; the rule permits it | unconfirmed |
| X4 | holder | My | Change Card properties or the access rule | `delegated_access_update` | Refused with a named code, because the project route binds the selection only; nothing changes | unconfirmed |
| X5 | member | own My | Link my GitHub key to the project | Hub `project_person_github_key_link` (MyCardGithubSection) | The key is linked for this project; if it changes My Card, through the project; GitHub tokens then issue for the person's agents | unconfirmed (found by the W730 guard, 14:26) |
| X6 | member | own My | Unlink my GitHub key | Hub `project_person_github_key_unlink` | Unlinked; if it changes My Card, through the project; token issue stops | unconfirmed (found by the W730 guard, 14:26) |
| X7 | member | own My | Set my commit email for the project | Hub `project_person_commit_email_set` | Saved; if it changes My Card, through the project | unconfirmed |

### Revoking and removing

| ID | Actor | Card | Action | Screen request | Expected | Status |
|---|---|---|---|---|---|---|
| V1 | admin | C | Remove a person from the project | Hub `project_person_control_revoke`, or PB `project.people.remove` | C is revoked through the project and the person's membership ends | unconfirmed; the Hub request refuses outright today (W728) |
| V2 | holder | managed Card | Revoke the Card from the Cards screen | Hub `delegated_access_revoke` | Through the project where the rules allow it; otherwise a named refusal the screen explains | unconfirmed; refuses today (W729) |
| V3 | admin | P | Revoke the project Control Card | `delegated_access_revoke` | Refused by design with a named reason (`managed_card_revoke_project_control_unsupported`): P exists as long as the project and ends only when the project is deleted. Operator, 11 Oct 15:16Z, verbatim: "no. if we have project we have card. when project deleted then card as well" | refusal confirmed (W730); message W729 |

### People and roles (Problem Board screen)

| ID | Actor | Card | Action | Screen request | Expected | Status |
|---|---|---|---|---|---|---|
| P1 | admin | I | Invite a person | PB `project.people.invite` | I created through the project | unconfirmed |
| P2 | invitee | I, then C and My | Redeem the invitation | Hub redemption | C and My created; membership starts | unconfirmed |
| P3 | admin | C + My | Fit to role (preview, then confirm) | PB `project.people.control.backfill` (the Control step, first), then `project.people.card.reset` (FitToRoleLever: preview and confirm); `project.people.set_role` for a role change from the people list | The role minimum is set on both Cards through the project; other selections are kept | unconfirmed at the request layer; transaction layer PASS (W732 14:13) |
| P4 | admin | C | Read a person's Card from the people list | PB `project.people.card` (read) | The person's C as the board shows it | unconfirmed. Note: PB `project.people.card.update` is retired (410 `work_control_card_edit_in_connection_hub`, W732 14:18); a person's C is edited only in the Hub (S1) |
| P8 | admin | C (several people) | Move people to Cards | PB session operation `project_people_cards_move` | Each person's Cards are created or moved through the project | unconfirmed |
| P5 | owner | C + P | Transfer ownership | PB `project.people.transfer_ownership` | Through the project | unconfirmed |
| P6 | admin | P | Set the access rule (AND or OR) | PB `project.control.update` | Through the project; OR asks for confirmation | unconfirmed |
| P7 | admin | P | Initialize P | PB `project.control.initialize` | P created, linked to the project | unconfirmed |

### Agents (Problem Board screen)

| ID | Actor | Card | Action | Screen request | Expected | Status |
|---|---|---|---|---|---|---|
| A1 | owner or admin | A + P | Add an agent to the project | PB `project.link_worker` | P is linked to A through the project | unconfirmed |
| A2 | owner or admin | A + P | Unlink an agent | PB `project.unlink_worker` | Attendance ends, then the P link is removed in the same step; A applies unchanged | unconfirmed |
| A3 | person | A | Make an agent the coordinator, hand over, return | PB `project.coordinator.*` | The coordinator profile is applied to A through the project | unconfirmed |
| A4 | admin | A | Evict, retire or restore an agent | PB `worker.evict`, `worker.retire`, `worker.restore` | Attendance status changes (evict = limbo, restore = active); the agent Card and its P link are unchanged (only unlinking removes the P link, A2) | evict/restore driven PASS (W733 14:33); retire unconfirmed |

### Enforcement (reading the Cards on every call)

| ID | Actor | Action | Expected | Status |
|---|---|---|---|---|
| E1 | person | Call a board operation | Allowed only if P op (C op My) allows it; a refusal names the Card | live: authorize lines logged, for example 13:44:28 |
| E2 | agent | Call a board operation | Allowed only if A and P allow it (AND) | unconfirmed |
| E3 | agent | Call while P is missing, revoked or unreadable | Refused; never falls back to A alone | unconfirmed |

## Keeping this list true

- A new Card-changing request on either screen needs a row here and a row in the standing test before it
  ships. The test fails when a screen gains a Card-changing request that no row names.
- A row's Status changes only with evidence: a test run on the exact head, or the operator's live result with
  its time.
