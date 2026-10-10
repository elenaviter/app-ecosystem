# Card transactions (W502)

A Card edit that must agree with another application's state (Problem Board's
project roles, its last usable administrator) commits as one transaction
across both, through the W581 v2 kernel in `service_foundation.coordination`.
This page lists what the Hub provides, how it is configured, and what must
hold before it is switched on. Everything here is **off by default**:
`connections.card_transactions.enabled` is false unless it is exactly `true`.

## What the Hub provides

| Piece | Where | What it does |
|---|---|---|
| Hub participant | `cards/card_participant.py` | Stages one Card change behind a fence, and commits or aborts it on the coordinator's decision. |
| Read reservations | `cards/transaction_store.py` | `card:<sh>:<id>` = revision and `card-absent:<sh>:<id>` = 1 hold the Cards an initiator's check read, from prepare until the decision. |
| Catalog reservation | `catalog/reservations.py` | `catalog-active:<sha256(canonical {content_hash, version})>` = 1 holds the active catalog. `ensure_delegated_catalog` refuses `catalog_reserved` (retryable) meanwhile. |
| Participant endpoint | `card_transaction_participant` (route public) | Another application's coordinator drives `prepare`, `finish`, `read_pending` and `list_prepared`. Every authenticated answer is signed. |
| Census read | `card_census_read` (route public) | A signed, optimistic read of a project's person Cards, their full Control chains and the active catalog, for the initiator's business check. |
| Lifecycle plan | `card_lifecycle_plan` (route public) | A signed, non-writing plan of one Card lifecycle change (a new project's P, C and My; a person joining; an invitation's redemption): the exact Card candidates the initiator freezes into its transaction. Same peer authentication as the census read; a caller plans only under its descriptor's `plan_scope_prefix`. |
| Card version save | `card_version` (route public), `cards/participant_card_version.py` | W661: Problem Board's STAGE, PUBLISH and ROLLBACK of one Card save. See "Saving a Card version" below. |
| Plan authorization | `cards/lifecycle_plan_authorization.py` | Before planning, the Hub asks the calling application's own host (`project_lifecycle_plan_authorize` on its authority bundle) for one decision per step. The body is `actor_subject`, `project_ref`, `request_id`, the plan's `request_digest` and the ordered `steps`, signed with the descriptor's authority request signer under protocol `card-lifecycle-plan-authorize.v1`. A missing, extra, repeated or mismatched step, or a step decision for another plan's digest, refuses the plan. |
| Managed Card edit forward | `managed_card_edit_forward.py` | A managed edit a direct writer refuses is forwarded, signed, to the project host that plans its scope (`project_card_edit`), which makes it in its own transaction. |
| Decision routing | `RoutedDecisionPort` | A Card staged by another application's transaction reads its decision from that application's authority. |
| Recovery | cron `card-transaction-recover` | Finishes or presumes-aborts in-doubt Hub transactions, page by page, with the cursor kept in Redis. |

**How the Hub calls a host's signed operation.** This applies to plan
authorization, the transaction authority fetch (`cards/authority_transport.py`)
and the managed Card edit forward:

- **Send.** The signed body travels whole under the operation's `data`
  argument, with `user_id` and `fingerprint` given as `null`. Otherwise the
  platform fills those two from the signed-in session, and the host's
  exact-body check refuses the extra field.
- **Read.** The answer is read through `normalize_bundle_operation_result`.
  Inside a request, the operation route wraps it as
  `{status: ok, <operation>: answer}`; a local call returns it bare. A
  wrapper outside that contract is an invalid answer, never an allow or a
  refusal.

## Saving a Card version (W661)

`card_version` serves Problem Board's save. PB calls the Hub, and the Hub calls
nothing back during the save. It has the same peer authentication and
`plan_scope_prefix` entitlement as `card_lifecycle_plan`. The request schema is
`card-version-request.v1`, and every authenticated answer is signed
(`card-version-answer.v1`, `AnswerContract.CARD_VERSION`). The answer echoes
only `op`, `request_echo`, `scope` and `txn`. Its `request_digest` binds the
whole request, so no edit value is repeated.

- **`op: stage`.** The request carries `txn`, `request_id`, `at` (the
  request's own time, ISO 8601 with an offset; a retry sends the same), the `catalog`
  (`version`, `content_hash`) PB authorized under, the actor, PB's
  `delegable_grants`, the project Control locator, and the planner's
  `creations` and `updates`. An update's `original_revision` is the base
  version.
  - PB authorized the save by role before STAGE. The Hub turns PB's grants into
    one allow per step and builds the new versions with `plan_card_lifecycle`.
    A base that moved is `card_changed`, and another catalog is
    `stage_catalog_moved`.
  - The Card store then writes each version once, not yet final, under the
    Card's lock. It compares a retry by the STAGE digest: every request field
    except the per-call `request_echo`. A different edit under the same `txn`
    is refused `stage_txn_conflict`.
  - A person's C or My that already exists on its stable id, revoked or
    active, is created again at its next revision. Operator, 10 Oct: "UPSERT.
    overwrite".
  - The answer holds only links: `{card: {subject_hash, access_id}, version,
    checksum}`.
- **`op: publish` and `op: rollback`.** These carry `txn` alone, because the
  store's transaction marker names its members. ROLLBACK answers
  `rolled_back`, `already_published` or `unknown_txn`.
- **Binding.** STAGE records the authenticated caller and scope in the
  marker. A replay, PUBLISH or ROLLBACK from another caller or scope is
  refused `request_scope_invalid` and touches nothing.
- **Effects.** A person Card save has none. An edited agent Card has only
  `handle_binding`:
  - its row identity is checked, and an agent's re-wrap prepared, at STAGE;
  - it is applied at PUBLISH only while `current.json` names this txn's
    version, and is otherwise `superseded`;
  - a ROLLBACK of a staged txn discards the prepared re-wrap.

  Any other effect kind refuses the save (`edit_invalid`). The store records
  each effect's named result in the marker, so a retried PUBLISH applies
  nothing twice.

## The person Control under the project Control (C -> P)

A person's project Control Card C (the per-person Card a project admin edits)
is bound under the project's own Control Card P, the application Control its
creator holds at `control_card_id_for_issuer("application", <project>, grantor_subject=<creator>)`.
The chain is then My -> C -> P, and a project-level AND in P denies what any
lower OR would allow.

- **The host names P.** Problem Board answers P's `control_id` and
  `holder_subject` from its own stored link, in the membership answer behind
  every project-person decision and in the invitation binding evidence
  (`ProjectControlLocator`). Nothing in a request selects P. The derived id is
  a consistency check only: a locator that is not P's derived id is refused
  `project_control_locator_mismatch`.
- **No unbound C.** Create and invitation redemption write C already bound
  under P in its first revision (`project_control_binding.bound_at_creation`):
  the same binding an attach records, the whole chain composed before the
  write, and the write gated as a `create` of a bound Card, so the binding's
  writer policy decides it. P is checked before anything is written, so an
  absent P (`project_control_absent`), an ended or foreign one, or a P under
  another Card (`project_control_not_root`) refuses with no C written and no
  invitation consumed.
- **An existing C** (found by create, an exact redemption retry, or the repair
  operation `project_person_control_bind_project`) is bound through
  `project_control_binding.bind_project_control`, which attaches through
  `attach_control_card`: the caller-writer gate decides it as an attach.
- **The qualified edge.** C -> P crosses holders (C is held by the project
  authority subject, P by its creator). Only that exact edge is qualified:
  P's id, holder, `application` issuer kind and project must match the binding,
  and P's own operation (AND or OR) applies. Any other cross-holder edge keeps
  the generic rule (an OR there is refused `control_card_foreign_holder_requires_and`).
- **P is the top boundary** (Root, option (a)). A P with a parent is refused at
  bind, and a bound C whose P gains a parent is invalid
  (`project_control_not_root`), never extended. No other application Control
  is restricted.
- **Idempotent, no reset.** The same P again is `already_bound`. A C bound to
  another live P is `p_conflict`; it moves only after that P has ended. Editing
  P never rebinds C. Nothing detaches an ancestor or resets a person's
  selections.
- **Repair report.** `project_person_control_bind_project` (operation
  `project.person_control.bind_project`, decided by the host) answers each
  person's `outcome`: `bound`, `already_bound`, or a refusal (`p_conflict`,
  `p_invalid`, `control_missing`, `not_bound`), so a migration can show every
  person bound.

The census stays generic: a chain that ends at C is `complete` [C], and the
initiator's provider decides whether that chain is anchored at its P.

## Proposing a Card group without changing saved Cards

`plan_card_lifecycle` in `delegated_credentials/card_lifecycle_plan.py` builds
the Hub's `connection-hub.card-group.v1` candidate and participant input from
one authorized project scope. The caller supplies catalog selections without
placing project roles in the Hub;
the Hub resolves them against its active catalog and constructs the same P
and C authority values used by the live constructors. The request has local
creation refs, kinds `application_control`, `project_person_control`, and
`project_person_my_card`, and parents that name either an earlier planned ref
or a live `{access_id, holder_subject}`. Updates name an exact current Card
revision and either revoke it or attach an existing C under P.

The planner returns canonical sorted members, the active catalog digest, and
every live parent outside the group as a present read at its actual revision.
New Card slots are group targets with absent originals; they are never
dependency reads. An existing target carries its actual original revision.
The Hub composes the entire proposed Control graph, including parents being
created in the same group, before returning the plan. A missing, ended,
foreign, cyclic, or otherwise invalid parent refuses the proposal. Staging
checks that graph and all read/catalog dependencies again under the Card
transaction fences; the proposal itself reserves nothing.

This lets one group carry C and My joining a live P, P/C/My genesis with
no temporary live P, C/My creation plus pending invitation Card revoke, or a
qualified repair attach. It does not save a Card, issue a credential, consume
an invitation, write a project role, or rewrite a person's existing My Card.
The signed plan operation binds the complete request digest to a
`LifecyclePlanAuthorization` envelope. Each creation ref and each
`update:<index>` has its own exact operation, target and bounded
`ProjectAuthorizationDecision`; a P step's grants cannot enlarge C's or My's.
The planner matches the envelope's complete step list before reading the
catalog and builds each candidate under only its own step's grants, platform
flag and P locator. The caller binds the signed proposal to its transaction.
A successful plan is input to prepare, not permission to commit.

## Editing a managed Card from the Hub

While enabled, a person's Save of a person Control bound under a project's
Control (`project_person_control_update` for a person, not a pending
invitation) is not written by the Hub. The Hub forwards it to the project
host that plans that scope, and the host makes it in its own transaction:

- **What travels.** `{schema: managed-card-edit.v1, actor_subject, project_ref,
  request_id, target: {kind: person_control, principal_key, original_revision},
  selection}`. The actor is the person the Hub authenticated, from the platform
  session only. `selection` holds only the fields the person changed
  (`resource_grants`, `resource_operations`, `named_service_operations`,
  `account_scope`); the host keeps every other field of the revision-fenced
  original, never an empty default. A Save that changes properties or the
  composition, or names no revision, refuses before anything is sent.
- **How it is sent.** The signed body travels whole under the operation's
  `data` argument, with `user_id` and `fingerprint` given as `null`. The
  platform otherwise fills those two arguments from the signed-in session, and
  the host's exact-body check refuses the extra field
  (`work_managed_card_edit_request_invalid`).
- **How it is signed.** The Hub's admission proof under the caller
  descriptor's authority request signer, with its own protocol
  (`managed-card-edit.v1`) and operation (`project_card_edit`), so it never
  verifies as a plan authorization or an authority answer. The host checks the
  signer, the exact body and a single-use nonce, then decides the person's
  role and Card authority itself.
- **What comes back.** `{ok: true, outcome: {schema: managed-card-edit-outcome.v1,
  request_id, state, transaction_id, card_revision}}`, read from inside the
  operation route's envelope (`{status: ok, project_card_edit: …}`). A refusal
  keeps the host's own code, status and message. The Hub logs the target kind,
  that code and that status. Only `committed` saved
  the edit. `aborted` and `pending` keep the person's draft. A lost answer is
  `managed_card_edit_outcome_unknown`: retrying the same `request_id` with the
  same edit replays the host's one decision; a different edit under that
  `request_id` refuses.
- **Without a forwarder** (no plan scope, or the signer secret unavailable)
  the writer keeps refusing `card_transactions_direct_write_refused`.

**The per-service Reset to Control** is not a Hub operation: it is a button
on the person's own card in Problem Board, and the project host makes it in
its own transaction. The Hub supplies only the pure, shared
`managed_card_reset.managed_card_reset_display`, so the host computes the
exact shown result with the Hub's own composition and `reset_candidate`.

The editor sends a Card's properties back with every Save: only a copy equal
to the stored person Control may travel with a forwarded edit.

**A project agent Card's Save** (`project_agent_card_update`, once the host
authorized the person) is forwarded the same way with `{kind: agent_card,
access_id, subject_hash, original_revision}` when the stored Card is bound
under a Control. Only its selection travels; a changed label, composition or
properties refuses before anything is sent. The host plans it with
`reselect_agent_card` (`managed_card_selection_plan.py`), which accepts only a
Card bound directly under the scope's project Control, and its display kind is
`agent_card`. `reselect_project_control` plans an edit of the project Control
itself under `project.control.update`.

**A pending invitation's Control Save** (`project_person_control_update` with
`invitation_ref`) is forwarded the same way with `{kind: invitation_control,
access_id, subject_hash, original_revision}`; the host plans it with
`reselect_invitation_control` under `project.invitation_control.update`, and
the Card keeps its invitation identity marker. Without a forwarder the writer
loads nothing and keeps refusing.

The project Control's own Save is not forwarded yet; it stays refused while
enabled.

## Disconnecting a connected account

While enabled, a person's disconnect of a connected account is one durable
transaction, and the account is deleted only after its COMMIT:

- **The account is fenced first.** The Hub reads the account's incarnation,
  fences the account for this transaction, and lists the Cards that bind it
  again. A Card that gained the binding meanwhile aborts the disconnect
  (`card_account_binding_changed`, retryable). While the fence stands, a write
  that would add this account to any Card refuses `card_account_reserved`.
- **When Cards bind the account**, each unbound Card is a member of a
  `connection-hub.card-group` with its candidate minus that account, and the
  group carries the `account_delete` effect. A Card bound under a Control
  refuses the whole disconnect: its owner's transaction must change it.
- **When no Card binds it**, the same decision carries the effect alone, as
  the versioned `connection-hub.card-effects` input
  (`cards/card_effects.py`). Its candidate is
  `{"schema": "connection-hub.card-effects.v1", "subject_hash", "effects"}`:
  one owner and 1 to 4 `account_delete` effects, sorted by key, each with an
  empty `access_id`. The projection has `action` `effect`, both revisions 1,
  and no dependencies. An empty input or any other effect kind is refused,
  and a peer's authority can never send it.
- **STAGE** holds the exact incarnation, so a reconnection refuses
  `account_disconnect_pending` until the decision. **COMMIT** deletes only
  that incarnation and pins the outcome. **FINISH** releases the fence only
  after the deletion (or, on ABORT, after the hold is released). A replay
  returns the pinned outcome and never deletes a later reconnection.
- **Account records share one lock**, a Redis key per user and account
  (`RedisAccountLock`, taken by the app and by every bundle through
  `from_connection_hub`). A wait that times out refuses the write
  (`account_lock_timeout`), and a section that overruns its budget is
  interrupted. Each logs one WARNING, `[connection-hub.account-lock]`, with
  the key digest only, so lock contention shows in the logs.

## Issuing an OAuth client's original credentials

While enabled, an authorization-code exchange grants its Card (or changes it)
and issues that Card's credentials under one decision. No credential is usable
before that decision commits and its effects apply. The Hub never receives a
bearer, an authorization code or a PKCE verifier.

1. **`begin_oauth_issuance`** plans the Card that `record_oauth_grant` would
   write, using the same code and the same consent inputs. It stores the plan
   once and begins the decision from it.
   - The plan is in `connection_hub_oauth_issuance_plans`. The first writer wins,
     and every deadline is read from PostgreSQL's clock.
   - A first consent is a one-member `connection-hub.card-group` with action
     `create`. A later consent is the single-Card transaction with action
     `oauth_grant`.
   - The plan carries two `credential_issue` effects, `access` and `refresh`,
     each `{access_id, slot, expires_at, card_revision}`.
   - It returns an `OAuthIssuancePlan`:
     - the transaction and intent digest;
     - the Card, grantor, client, credential issuer and credential subject;
     - the base and planned revisions, the planned revision's expiry and
       content hash;
     - each slot's effect digest;
     - two deadlines: `delivery_deadline`, the earlier of the Card's expiry and
       600 seconds after `begin` (`ISSUANCE_DELIVERY_SECONDS`), and
       `reserved_until`, never later than the decision's own expiry.

   The same original request with the same inputs returns the same plan,
   deadlines included. Other inputs refuse `issuance_replay_changed`.
2. **`reserve_oauth_issuance`** stores one minted credential's SHA-256 digest
   and its non-secret record in `connection_hub_oauth_issuance_reservations`,
   one row per `(transaction, slot)`.
   - Only the plan's `transaction_id` selects. Any other differing field
     refuses `issuance_plan_mismatch`, and every trusted value is copied from
     the stored plan.
   - The record must name the planned Card, issuer, credential subject, grantor
     and client, and carry no secret field.
   - Readers take authority from the record and its credential envelope, so
     both must carry exactly the plan's `operations`, `resource_grants` and
     `resource_operations`. That is the candidate Card's own declared-key
     snapshot, fixed at `begin`. A missing, malformed, wider or concrete-URL
     value refuses `issuance_record_authority_mismatch`.
   - A reservation is in no table a reader looks at.
   - It is refused once the decision is decided or `reserved_until` has
     passed, and a retry never renews either.
3. **`complete_oauth_issuance`** prepares, commits and finishes the decision.
   - Completions of one transaction are serialized across processes and
     machines by a short claim on its plan row. The claim (`completing_owner`,
     `completing_until`) is taken, renewed and released by single committed
     statements, so no database connection is held across the completion's
     own calls.
     - Another live claim answers `pending` at once; a dead holder's claim
       lapses after 60 seconds.
     - The holder renews the claim before COMMIT and before ABORT. A lost
       claim decides nothing.
   - Only a named refusal records ABORT. Any other failure leaves the decision
     undecided for a retry or for recovery.
   - The decision passes through the enlisted caller-writer gate exactly as
     `record_oauth_grant`'s write does.
   - **STAGE** binds each reservation to its effect; a missing one refuses, and
     the decision aborts.
   - **COMMIT** activates each reservation: it inserts the real family and
     generation, or the access binding, under the Card's family lock. It does
     this only while the authoritative Card is exactly the committed revision
     (its content hash), active and unexpired. Otherwise the slot ends
     `superseded` and nothing is created, even when the Card has no family.
   - A token's expiry is its reservation instant plus the minter's lifetime,
     capped by the Card's expiry, so a late activation never extends it.
   - **ABORT** releases every reservation, and a reservation STAGE never bound
     expires at its deadline (`expire_oauth_issuance_reservations`, a
     scheduled sweep).
   - It returns an `OAuthIssuanceResult` naming the same transaction and intent
     digest: `committed` (with the Hub's committed receipt digest) only once
     every slot has applied, `aborted`, or `pending`. On `pending` the caller
     calls again; it never mints again.

**`read_oauth_issuance`** (and `read_oauth_issuance_plan`) return the
outcome (and the plan) as they stand. They are read only: they never prepare,
decide, finish or claim, so a caller recovering an uncertain response can read
before every slot is reserved without aborting the issuance.

**`read_oauth_issuance_plan_by_request`** (W585) returns the same plan for a
caller that lost `begin`'s answer before it kept the transaction id. It takes
only the plan's `decision_request_id`, the field-tagged hash of
`(scope, grantor_subject, client_id, original_request_id)` that the caller
keeps before `begin`. It never takes candidate inputs and is equally read only.
No stored plan for that request answers `issuance_plan_unknown`. A plan whose
decision `begin` never bound answers `issuance_plan_unbound`, and nothing can
be reserved or completed for it.

`delivery_deadline` bounds only the recovery of the original response to a
consumed exchange. The authorization code itself lives 60 seconds and is
validated on the first exchange. Keeping the raw bearers in encrypted custody
until that deadline is the SDK's part. So is activating the platform session
only after a committed result.

## Configuration

All of it lives in the Hub's bundle props under `connections.card_transactions`.
Secret values are referenced by secret path; they are never inlined.

```yaml
connections:
  card_transactions:
    enabled: false                     # true only in an approved activation window
    callers:
      <peer service id>:               # the admission proof's service_id, e.g. Problem Board's
        request_secret_ref: <secret path>     # verifies the peer's admission proof
        receipt_secret_ref: <secret path>     # signs the Hub's answers (may be the same key)
        receipt_signer_id: <Hub signer id>
        audience: problem-board@1-0           # the peer's bundle, bound into every answer
        hub_resource: connection-hub@1-0      # the Hub bundle the peer's proof names
        scope_field: project_ref              # intent payload key = request scope
        census_scope_prefix: "work:project:"  # scopes the peer may census-read; absent = none
        authority:
          service_id: <peer authority signer id>
          audience: connection-hub@1-0
          secret_ref: <secret path>           # verifies the peer authority's responses
          request_signer_id: <Hub service id>
          request_secret_ref: <secret path>   # signs the Hub's requests to the authority
          binding:
            bundle_id: problem-board@1-0
            operation: project_card_transaction_authority
            response_key: authority
            refusals: {<peer code>: <Hub reason>, ...}
            request_fields: {}               # optional fixed request fields; never the scope_field
```

A caller whose descriptor is malformed, or whose secret is missing or shorter
than 32 bytes, is left out. Its requests are then refused as an unknown caller.

## Activation conditions

Before `enabled: true`, every one of these must be true. They are qualification
obligations, not optional checks.

1. **PostgreSQL authority.** The Hub's delegated authority uses PostgreSQL. The
   decision store (`connection_hub_card_decisions`) is created on first
   authenticated use.
2. **The peer's side is live and pinned.** Problem Board's writer uses the v2
   authority (`{ok, authority}`), its consumer verifies the shared vectors,
   and its census provider and admin check use the ACTIVE catalog and emit
   the `catalog-active` key.
3. **End-to-end test passes.** A Problem Board-initiated commit, abort, refused
   prepare followed by abort, and crash recovery all succeed through
   `card_transaction_participant` on the deployed Hub.
4. **Unbound Card stores fail closed.** `RoutedDecisionPort` is bound only where
   `card_transaction_coordinator` runs. A Card store built outside it reads a
   Card staged by Problem Board as `card_transaction_undecided` until the Hub
   finishes it. That is fail closed, not wrong, but it means a reader may see
   "being created / undecided" briefly.
5. **Catalog publication may wait.** While a transaction holds the active
   catalog, a deploy that would publish another version fails with
   `catalog_reserved`, logging the holding transaction ids. Retry after the
   transaction is decided. A crashed publisher's marker is cleared by the
   next publication, or by the recovery cron once the runner lock is stale.
6. **Census completeness and decoding are the caller's.** `card_census_read`
   answers only for the persons named. The initiator takes the person list
   from its own fenced membership census, and a missing person must fail its
   check. An answer is capped at 512 KiB minus 4 KiB: anything larger is a
   signed 413 `card_census_too_large`, and the caller subdivides its persons.
   The caller must decode every person state the Hub signs, tested from the
   Hub's own projection: edge `valid`, `invalid` or `missing`, and chain
   `complete`, `missing`, `in_transaction`, `unavailable` (retry) or
   `invalid` (persistent: that person is not usable).
7. **Person-owned settings never travel.** The census sends only properties
   classified as authorization in `card_property_classes`. A new property
   key must be classified before it can travel.
8. **Every person is bound under P first** (EMain R2). Create and redemption
   bind C under P is deployed first; then the repair binds every existing
   person and its per-person report shows each one `bound` or
   `already_bound`; only then is this switched on. Until then a census chain
   ends at an unbound C, which the initiator's anchoring check refuses.
9. **Managed project Card writes go through the project's transaction.**
   While enabled, the Hub's direct managed writers refuse finitely, before
   writing anything, with
   `card_transactions_direct_write_refused`, never retryable: status 409 with a
   message from a writer's own guard, or status 403 with no message from the
   shared persist check (`_persist_record`, which covers bound Cards and a
   managed P). A caller keys on the error code, not the status:
   - `project_person_control_create`, `project_person_control_update` and
     `project_person_control_revoke`, for a person and for a pending
     invitation;
   - `project_person_control_bind_project` and
     `project_person_control_bind_invitation`;
   - `project_person_my_card_seed`;
   - `revoke_access` of any Card bound under a Control (a person's My Card,
     a project-bound agent Card);
   - any other write to a Card bound under a Control, before or after the
     write, that creates it, replaces its credentials, moves its expiry back
     or changes an authority field (grants, operations, named services,
     account scope, identity scope, properties, composition, binding, state,
     identity). A prolongation and an edit of display fields still pass, and
     a bound Control's legacy snapshot migration is used in memory only;
   - every direct write of a project's Control Card P: `control_card_create`
     (including starting or repairing an existing P), `control_card_update`
     and the `project_control_card_update` alias, `control_card_revoke`,
     `update_access` and `revoke_access` of P, and attaching or detaching P
     (above or below another Card, `project_control_card_attach` and
     `project_control_card_detach` included). P is the stored application
     Control at the id derived from its project and holder, in a scope a
     configured caller plans (its `plan_scope_prefix`). An application
     Control outside every such scope is unchanged, and reading P stays a
     read.

   These writes are made only by a transaction the project host coordinates:
   it plans with `card_lifecycle_plan`, then prepares and finishes through
   `card_transaction_participant`. A person-Control Save reaches that
   transaction through the managed Card edit forward (above). That coordinated path must be live before
   this is switched on. Until then, enabling refuses these edits.

   **The one exception: the legacy binding repair at bundle load** (P0, 10 Oct 2026). Live, this was
   switched on before rule 8's repair had run, so every person's C still had no binding. The Card-edit
   PLAN (`existing_card_selection_plan._target_identity`, unchanged) then refused every save with
   `card_plan_update_scope_invalid`. `on_bundle_load` now runs
   `legacy_binding_repair.repair_legacy_project_bindings`. For a project with exactly one active root P,
   it binds each active unbound C under that P through `bind_project_control`, then refreshes each active
   My Card's pointer with the repair `ensure()` makes. Only that run sets
   `automation_access.LEGACY_BINDING_REPAIR`; no operation, request or descriptor can. While it is set,
   W578 allows exactly two things: attaching an unbound C to its own project's root P, and a write to a
   bound Card whose only changed authority field is `control_card`. Both write the binding a new Card
   gets at creation. A My Card is repaired only when its project has exactly one root P and its C is
   bound to that P. Writes stay fenced, it logs counts only, and a failure never blocks the load. A run
   that leaves nothing outstanding writes `legacy-binding-repair.complete.json` beside the Card store;
   every later load reads only that file and no Card. Any skip, refusal or error leaves it unwritten, so
   the next load retries.
10. **Removing v1 waits.** The v1 authority path is removed only after the peer's
   writer switch.

Turning it on, changing secrets or peer ids, and restarting are a coordinated
runtime window, approved by the operator.
