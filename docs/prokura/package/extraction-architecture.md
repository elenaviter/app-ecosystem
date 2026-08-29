---
id: prokura-package-extraction-architecture
title: Prokura Package Boundary And Extraction
summary: Defines the authority domain Prokura owns, the host-runtime ports it consumes, and the completed source cutover from KDCube without changing delegated-access behavior.
tags:
  - prokura
  - architecture
  - delegated-access
keywords:
  - authority register
  - identity card
  - per-call admission
  - host adapter
  - KDCube
see_also:
  - ../README.md
  - ../connection-hub-architecture.md
  - ../frontend/README.md
  - ./delegated-authority-and-admission.md
  - ./delegated-cards.md
  - ./oauth-delegated-credential-protocol.md
---

# Prokura package boundary and extraction

Prokura owns the delegated-access authority. A caller presents its identity
and the operation it wants; Prokura resolves that request against the current
registered card and capability catalog on every call. Editing or revoking a
card therefore changes the next decision without changing a token carried by
the caller.

The running implementation was extracted from KDCube without changing its
authorization semantics. The move used continuously testable slices so the
same implementation, rather than a fork or simplified rewrite, became the
package implementation.

**Current migration state:** the portable authority, persistence orchestration,
connected-account lifecycle, and admission state machines are owned by
`prokura`. KDCube consumers import portable contracts directly from that
package and use focused host adapters under
`kdcube_ai_app.apps.chat.sdk.integrations.prokura`. The historical KDCube
connections implementation has been retired. The Connection Hub frontend
lives in this repository and is registered into KDCube by Git repository, ref,
and subdirectory.

## Ownership boundary

Prokura owns:

- actor, account, resource, claim, and operation contracts;
- durable identity-card and capability-catalog histories;
- rebuildable serving projections and exact card selection;
- card reconciliation, drift detection, and per-call admission decisions;
- delegated OAuth and automation-card lifecycle rules;
- structured denial and recovery contracts;
- persistence interfaces and the storage-independent state machines that use
  them.

A host runtime supplies adapters for:

- authenticated request and user-session context;
- configuration and server-side secret resolution;
- durable storage, Redis-compatible projections, and locking;
- application-operation dispatch and provider discovery;
- transport-specific consent events and result delivery;
- host-specific identity providers and connected-account brokers.

This boundary allows KDCube to remain one host while the same authority can be
embedded in another service or exposed by Prokura's planned standalone
service.

The Connection Hub application is intentionally a KDCube host adapter. Its
entrypoint imports KDCube lifecycle, HTTP, configuration, Redis, and Data Bus
facilities, while delegated-authority policy and state machines come from
Prokura. Host neutrality is therefore an invariant of `packages/prokura`, not
of every application that presents Prokura through a particular runtime.

## Migration invariants

The extraction preserves the production contracts already exercised in
KDCube:

1. Durable card/catalog revisions and pointers are authoritative; their caches
   and indexes are rebuildable projections. TTL-bounded OAuth records,
   credential handles, and replay state are live protocol authority and are
   not reconstructed from card history.
2. Admission resolves the current card and catalog for every invocation.
3. An exact access-card identity is selected before authority is evaluated.
4. An absent policy and an explicitly empty policy have different meanings.
5. Agent, OAuth-client, and automation callers receive structured refusals
   appropriate to their transport.
6. One implementation owns each model and registry at runtime. Transitional
   imports re-export that implementation rather than loading duplicate class
   or registry identities.

## Completed extraction sequence

The migration proceeded through continuously testable slices:

1. Move the closed, host-neutral model and state-machine core into `prokura`.
2. Replace direct host calls with explicit runtime ports and move the
   authority, OAuth, admission, and persistence orchestration.
3. Move the Connection Hub application into this repository as Prokura's
   KDCube-hosted frontend.
4. Make KDCube consume the package and register the application by repository
   path.
5. Retire the built-in Connection Hub app and historical KDCube connections
   package, then migrate consumers to direct `prokura` imports or the explicit
   KDCube host-integration namespace.

The package, Connection Hub backend and frontend, KDCube host contracts,
delegated-card caller-family matrix, and external application bundle contracts
have passed offline. The remaining deployment gate is a clean Git install into
a refreshed KDCube stand, followed by a live consent and card-management
journey. This gate requires the package and application changes to be committed
and reachable from the descriptor's Git reference.
