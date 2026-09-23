---
id: connection-hub/package/durable-authority-generations
title: "Durable Authority Generations"
summary: "How Connection Hub activates PostgreSQL authority, preserves complete live Card credential chains, inventories Redis state, and prevents restored Redis data from becoming authority."
status: current
tags: ["architecture", "connection-hub", "authority", "postgresql", "redis", "cutover"]
keywords: ["authority generation", "PostgreSQL authority", "Redis inventory", "cutover receipt", "Card credential chain", "OAuth continuity", "session projection"]
updated_at: 2026-09-23
see_also:
  - ./delegated-authority-and-admission.md
  - ./delegated-cards.md
  - ./oauth-delegated-credential-protocol.md
  - https://github.com/kdcube/kdcube/blob/main/app/ai-app/docs/recipes/operations/move-authority-to-postgresql-README.md
---
# Durable Authority Generations

Connection Hub serves long-lived credential authority from PostgreSQL. A
generation receipt binds one reviewed source preview to the exact PostgreSQL
state activated for a tenant and project. Runtime startup requires that receipt
before selecting the PostgreSQL backend.

This makes backend selection explicit and durable. Restoring an older Redis
snapshot cannot change current OAuth, Card-handle, replay, or session authority
after the descriptor selects an activated PostgreSQL generation.

An existing runtime with no authority backend selector remains on
`redis-migration-source`. An absent authority node, an empty authority mapping,
and a blank normalized backend all select that source mode with no generation.
An explicit backend value is validated, and PostgreSQL requires the activated
generation identifier from its receipt.

## Storage ownership

| State | Authoritative storage |
| --- | --- |
| OAuth client registrations, refresh generations, and access-token bindings | PostgreSQL; bearer values are represented by hashes. |
| Card-handle metadata and cleanup lifecycle | PostgreSQL. |
| Agent Card bearer retained for hosted reuse and network presentation | Deployment secret provider; PostgreSQL stores its reference and fingerprint. |
| Delegated Card identity, revision, state, and expiry | Durable bundle storage. |
| Admission replay claims | PostgreSQL. |
| KDCube bundle users, authority versions, bundle sessions, and platform sessions | PostgreSQL after activation. Redis keeps generation-scoped, TTL-bound read projections. |
| Rebuildable projections, short-lived coordination, replay fences, and traffic smoothing | Redis. |

The Connection Hub package owns the authority schema, preview evidence,
idempotent import, reconciliation, and activation receipt. A KDCube host owns
runtime composition, descriptor loading, PostgreSQL connectivity, Redis source
access, and secret-provider access.

## Reviewed cutover policy

The supported existing-runtime cutover preserves every active, unexpired Card
credential chain that is complete at preview time. One chain consists of the
Card-handle row, its active access binding, active refresh generation when one
exists, and the referenced dynamic OAuth client. An Agent Card instead requires
its reusable bearer and active access binding; the bearer remains in the
deployment secret provider for hosted reuse and can also be presented by that
agent over the network. The preview verifies the durable Card identity,
revision, state, expiry, and every required chain link before the chain can be
imported. A missing link is a named blocker; apply never turns an apparently
live Card into a disconnected Card.

Records outside a complete live chain start clean in the new generation:

| Source state | Result after activation |
| --- | --- |
| Browser and platform sessions | Users sign in once. Current roles, permissions, provider identity, and authority version are rebuilt by the login path. |
| Active, unexpired Agent Card with a complete credential chain | Card handle, reusable bearer, and access binding survive. This covers the same agent running under hosted runtime custody or presenting its credential over the network. |
| Active, unexpired OAuth Card with a complete credential chain | Card handle, access binding, active refresh generation, and referenced dynamic client survive. |
| Pending Card without an issued credential | Remains pending. It authorizes when activated; there is no live credential to migrate. |
| Expired, revoked, orphaned, or incomplete Card credential | Does not revive. An incomplete live chain blocks the preview; an expired or revoked credential is reset and must be issued again. |
| OAuth records not owned by a preserved live Card chain | Existing access and refresh tokens fail closed; unreferenced dynamic clients are reset. |
| Descriptor-declared public OAuth clients | Recreated from the active Connection Hub descriptor. |
| Admission replay claims and consumed one-time records | Begin in the new generation with no history from the retired Redis source. |
| Durable Card documents, grant policy, and capability policy | Unchanged in bundle storage, including when an expired credential is reset. |

Conversation data, app files, durable Card documents, and identity-provider
accounts are outside this reset.

## Redis family inventory

The migration preview scans the Redis families that currently carry durable
authority. Its `source_summary.preserved` and `source_summary.reset` buckets
count the decision for every record in these families.

| Redis family | Writer and reader | Lifetime | Cutover classification |
| --- | --- | --- | --- |
| `{tenant}:{project}:kdcube:oauth:client:*` | OAuth registration and client lookup | Persistent or registration expiry | Dynamic clients referenced by a preserved refresh chain migrate; all others reset. |
| `{tenant}:{project}:kdcube:oauth:refresh:*` | OAuth token issue, rotation, refresh, and revoke | Refresh expiry | Active generations owned by preserved Cards migrate; expired, revoked, missing-Card, and unreferenced generations reset. |
| `{tenant}:{project}:kdcube:oauth:agrant:*` | OAuth access issue and bearer validation | Access-token expiry | Active bindings owned by preserved Cards migrate; other bindings reset. |
| `{tenant}:{project}:kdcube:delegated-access:card-handles:*` | Card credential issue, renewal, and presentation | Card credential expiry | Complete live Card handles migrate. Agent bearer material moves to the secret provider; PostgreSQL stores its reference and digest. A descriptor sync credentials a legacy live Agent Card before preview; any live Agent Card still missing its bearer blocks activation. |
| `connection-hub:admission:{tenant}:{project}:nonce:*` | Admission claim and replay check | Claim expiry | Replay history resets by reviewed policy. |
| `{tenant}:{project}:kdcube:delegated-access:automation:*` and `control-card:*` | Legacy Card projections | Record expiry or durable lifecycle | Counted as reset; current Card and control authority rebuild from durable bundle storage. |

The following families remain Redis by design. They are excluded from migration
preview counts because they are rebuildable projections, coordination leases,
or bounded retry/replay-cost state; none becomes PostgreSQL authority.

| Redis family | Writer and reader | Lifetime | Classification and recovery |
| --- | --- | --- | --- |
| `{tenant}:{project}:kdcube:connection-edge:principal:*` | `ConnectionEdgeRuntimeCache.publish_edge/read` | Current Redis run; removed on unlink | Rebuildable projection of durable connection edges. A run-id fence rejects restored snapshots. |
| `{tenant}:{project}:kdcube:connection-edge:mutation-lock` | Connection-edge mutation path | 30 seconds | Coordination lease; expiry releases an abandoned mutation. |
| `connection-hub:one-time-state:*` (default) and `kdcube:connection-hub:{tenant}:{project}:delegated-to-kdcube:oauth-state:*` (delegated OAuth caller) | `RedisRunBoundPointerStore` and the flow that supplies its prefix | Requested TTL, at most one hour | One-use pointer to a secret-provider payload. Current-run fencing rejects restored pointers. |
| `connection-hub:device-proof:{scope}:nonce:*` and `:jti:*` | Device proof issue/claim | At most 300 seconds | Nonce and replay-cost state; loss requires a new proof. |
| `{tenant}:{project}:kdcube:oauth:device:*` | OAuth device authorization issue, approval, and polling | 10 minutes; attempt counters use their bounded window | Pending flow state; loss restarts authorization and grants no authority. |
| `{tenant}:{project}:kdcube:delegated-access:card:*` | Card cache mutation and read-through | Card expiry; credentialless projections last until reconciliation | Rebuildable projection. Durable Card revision/state is checked before use. |
| `{tenant}:{project}:kdcube:delegated-access:cards-by-grantor:*` | Card discovery index | No whole-key TTL; members carry expiry scores | Rebuildable index. Every member resolves through Card authority before use. |
| `{tenant}:{project}:kdcube:delegated-access:cards-epoch` | Card reconciler | Current Redis run | Projection-completeness marker, rebuilt by reconciliation. |
| `{tenant}:{project}:kdcube:delegated-access:cards-reconcile-lock` | Card reconciler | 60 seconds with renewal | Coordination lease. |
| `{tenant}:{project}:kdcube:delegated-catalog:*` | Catalog publication and read-through | Descriptor-configured, normally 300 seconds active and 3,600 seconds historical | Rebuildable catalog projection; active reads are run-fenced. |
| `kdcube:authorities:{scope}:*` | Authority discovery reconciliation and lookup | Configured residency; current-run epoch; reconciliation lock 300 seconds | Rebuildable projection of descriptor authority definitions plus coordination. |
| `kdcube:connection-hub:{tenant}:{project}:authenticators:v1` | Authenticator selector list/read | Normally 30 seconds | Rebuildable selector projection. |
| `{tenant}:{project}:kdcube:federated-idp:token:*` | Federated Data Bus token issue/verify | 15 minutes by default, at most one hour | Short-lived token registry. Loss fails closed and the caller obtains a new token. |

`connection-hub:caller-self` is not a Redis family. It is the public resource
identifier returned by the gateway's caller-self operation, so it has no Redis
writer, TTL, or migration treatment.

## Preview and activation contract

The preview contains aggregate counts, family counts, prerequisite evidence,
and hashes. It contains no bearer, token-bearing Redis key, user identity, or
record payload. Applying a preview requires:

1. the exact preview SHA-256 selected by the operator;
2. stopped source writers;
3. a fresh source inspection equal to the reviewed preview;
4. exact target family counts and logical generation after import.

The receipt is written last. An interrupted apply has no activation receipt and
can be rerun: imports are idempotent and refuse conflicting target content. An
exact rerun after activation returns the existing receipt without reading the
retired Redis source.

The generation identifier belongs in descriptors permanently. It names the
activated authority epoch; it is not a timestamp inferred from a database row
and it does not change on reads.

Use the KDCube operator procedure to create the preview, stop writers, apply
the reviewed artifact, update both descriptors, and restart the runtime.
