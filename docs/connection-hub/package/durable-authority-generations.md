---
id: connection-hub/package/durable-authority-generations
title: "Durable Authority Generations"
summary: "How Connection Hub activates PostgreSQL authority, preserves hosted caller credentials, resets reconstructable login and OAuth state, and prevents restored Redis data from becoming authority."
status: current
tags: ["architecture", "connection-hub", "authority", "postgresql", "redis", "cutover"]
keywords: ["authority generation", "PostgreSQL authority", "Redis reset", "cutover receipt", "resident Agent Card", "OAuth reauthorization", "session reset"]
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

## Storage ownership

| State | Authoritative storage |
| --- | --- |
| OAuth client registrations, refresh generations, and access-token bindings | PostgreSQL; bearer values are represented by hashes. |
| Card-handle metadata and cleanup lifecycle | PostgreSQL. |
| Hosted Agent Card bearer that the runtime must present again | Deployment secret provider; PostgreSQL stores its reference and fingerprint. |
| Delegated Card identity, revision, state, and expiry | Durable bundle storage. |
| Admission replay claims | PostgreSQL. |
| KDCube bundle users, authority versions, bundle sessions, and platform sessions | PostgreSQL after activation. |
| Rebuildable projections, short-lived coordination, and traffic smoothing | Redis. |

The Connection Hub package owns the authority schema, preview evidence,
idempotent import, reconciliation, and activation receipt. A KDCube host owns
runtime composition, descriptor loading, PostgreSQL connectivity, Redis source
access, and secret-provider access.

## Reset cutover policy

The supported existing-runtime cutover preserves the only credential that the
host cannot reconstruct: the bearer attached to an active hosted Agent Card.
The preview verifies that bearer's durable Card identity, revision, and expiry
before it can be imported.

Other source records start clean in the new generation:

| Source state | Result after activation |
| --- | --- |
| Browser and platform sessions | Users sign in once. Current roles, permissions, provider identity, and authority version are rebuilt by the login path. |
| OAuth grants and dynamic client registrations | Existing access and refresh tokens fail closed. Integrations authorize again; dynamic clients register again. |
| Descriptor-declared public OAuth clients | Recreated from the active Connection Hub descriptor. |
| Admission replay claims and consumed one-time records | Begin in the new generation with no history from the retired Redis source. |
| Non-agent and orphaned Card-handle rows | Reset. Current non-secret Card authority remains in durable bundle storage. |
| Hosted Agent Card credential handles | Preserved in PostgreSQL plus the deployment secret provider. Relays and hosted workers keep their credential. |

Conversation data, app files, durable Card documents, and identity-provider
accounts are outside this reset.

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
