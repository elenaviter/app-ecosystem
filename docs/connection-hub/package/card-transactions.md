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
| Decision routing | `RoutedDecisionPort` | A Card staged by another application's transaction reads its decision from that application's authority. |
| Recovery | cron `card-transaction-recover` | Finishes or presumes-aborts in-doubt Hub transactions, page by page, with the cursor kept in Redis. |

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
6. **Census completeness is the caller's.** `card_census_read` answers only for
   the persons named. The initiator takes the person list from its own fenced
   membership census, and a missing person must fail its check.
7. **Person-owned settings never travel.** The census sends only properties
   classified as authorization in `card_property_classes`. A new property
   key must be classified before it can travel.
8. **Removing v1 waits.** The v1 authority path is removed only after the peer's
   writer switch.

Turning it on, changing secrets or peer ids, and restarting are a coordinated
runtime window, approved by the operator.
