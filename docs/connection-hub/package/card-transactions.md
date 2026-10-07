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
| Plan authorization | `cards/lifecycle_plan_authorization.py` | Before planning, the Hub asks the calling application's own host (`project_lifecycle_plan_authorize` on its authority bundle) for one decision per step. The body is `actor_subject`, `project_ref`, `request_id`, the plan's `request_digest` and the ordered `steps`, signed with the descriptor's authority request signer under protocol `card-lifecycle-plan-authorize.v1`. A missing, extra, repeated or mismatched step, or a step decision for another plan's digest, refuses the plan. |
| Decision routing | `RoutedDecisionPort` | A Card staged by another application's transaction reads its decision from that application's authority. |
| Recovery | cron `card-transaction-recover` | Finishes or presumes-aborts in-doubt Hub transactions, page by page, with the cursor kept in Redis. |

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
   reading or writing anything, with
   `card_transactions_direct_write_refused` (409, not retryable):
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
   `card_transaction_participant`. That coordinated path must be live before
   this is switched on. Until then, enabling refuses these edits.
10. **Removing v1 waits.** The v1 authority path is removed only after the peer's
   writer switch.

Turning it on, changing secrets or peer ids, and restarting are a coordinated
runtime window, approved by the operator.
