---
id: connection-hub/package/delegated-cards
title: "Delegated Access Cards: Storage, Rendering, And Enforcement"
summary: "Canonical lifecycle of Connection Hub Cards: credential-backed callers, credentialless Control Cards, live composition and enforcement, and descriptor-drift reconciliation."
status: active
tags: ["sdk", "solutions", "connections", "connection-hub", "delegated-access", "cards", "grants", "mcp", "named-services"]
keywords: ["Delegated by KDCube", "AutomationAccessRecord", "resource_grants", "resource_operations", "application operations", "delegated role", "named_service_operations", "account_scope", "registry_access_id", "card authority", "control card", "effective authority", "descriptor drift", "grant lifecycle", "stable resident identity", "resource_acceptance", "multi-resource card", "card read model"]
updated_at: 2026-09-22
see_also:
  - ./delegated-authority-and-admission.md
  - ./oauth-delegated-credential-protocol.md
  - https://github.com/kdcube/kdcube/blob/main/app/ai-app/docs/sdk/solutions/connections/connection-hub-solution-README.md
  - https://github.com/kdcube/kdcube/blob/main/app/ai-app/docs/sdk/solutions/connections/delegated-accounts/delegated-accounts-README.md
---
# Delegated Access Cards

A Connection Hub Card is the user-visible form of one server-side
authorization record. A credential-backed caller Card answers:

- which user granted access;
- which agent, connected app, or manual automation received it;
- which KDCube resources, grants, and operations that caller may use;
- which connected provider accounts and claims that caller may use;
- when the grant was created, when it expires, and how it is revoked.

The card is the user's live delegated-authority record, not a cached
illustration of a token. Pointer-backed credentials identify the card, and the
managed guard resolves its current contents on each call. Editing or revoking
the card therefore changes the next call made with an already-issued bearer.
The deployment descriptor, published as the delegated catalog, is the ceiling
around that user decision: every governed call intersects the card with the
active catalog, so a withdrawn capability is denied without editing the card.
Any credential-backed caller Card may link one credentialless Card. The link
gives that Card its **control** role. The two Cards compose with `and` by
default or with `or` when the grantor selects it; the active catalog still
bounds the result.

The card is also not a copy of the entire current service catalog. It stores
the user's selected authority. Connection Hub separately reads the live
descriptor and provider catalogs to render choices around that stored
selection.

This page covers every Card owned by Connection Hub: manual automations,
hosted agents, external OAuth/MCP clients, and credentialless Control Cards.
Cards in **Delegated to
KDCube** represent connected provider accounts and use a different storage
lifecycle; see [Delegated Accounts](https://github.com/kdcube/kdcube/blob/main/app/ai-app/docs/sdk/solutions/connections/delegated-accounts/delegated-accounts-README.md)
and [Connection Hub Token Storage](https://github.com/kdcube/kdcube/blob/main/app/ai-app/docs/sdk/solutions/connections/connection-hub-token-storage-README.md).
Connection-edge and authenticator administration cards likewise project their
own stores; they are not delegated-access records.

```text
stored card selection                 current deployment catalogs
  what this user granted                what can be granted now
  resource_grants                       resources and grants
  resource_operations                   protected and application operations
  named_service_operations              namespaces and operations
  account_scope                         connected accounts and claims
  control_card                          optional link to another Card
             \                           /
              +---- Connection Hub -----+
                         |
                         +-- read-only card: stored decision, live labels
                         +-- create/edit form: live choices + stored selection
                         +-- runtime guard: composed Cards AND active catalog
```

## Card Families

All four families use `AutomationAccessRecord`, the same immutable revision
format, and the same lifecycle implementation. Their purpose and credential
retention differ. The three caller families appear in the central **Delegated
by KDCube** list. A Control Card is currently opened by exact id from the
application that gave it its control role; a future central listing is a view
over the same records, not another store or migration.

| `source` | Represents | How it is created | Credential material retained in the card record |
| --- | --- | --- | --- |
| `manual` | A script, service, or external automation whose operator copies a bearer. | `delegated_access_create`. | The raw bearer is returned once and is not retained. The record keeps `session_id` and `last_four` for revocation and identification. |
| `agent` | A hosted agent with deterministic identity `kdcube-agent:<app>:<agent>`. | Demand-driven consent, or descriptor synchronization for a resident agent whose Card selects from a Control Card. | A consented agent retains its reusable access token server-side. A descriptor-synchronized capability Card retains no bearer; it carries the user's selection and a bounded inactivity lease. Token material is never returned by list. |
| `oauth` | An external OAuth/MCP client. | Automatically on initial token issuance and every refresh rotation. | Current access- and refresh-token handles are retained server-side so revoke can invalidate both. They are never returned by list. |
| `control` | A reusable authorization rule linked to one or more caller Cards. | An owner-scoped `control_card_create`, normally initiated by the application that will link it. | None. It has no delegate, bearer, refresh token, session, or expiry. |

An OAuth client and a hosted agent are both delegated callers. The source
field records how their credential lifecycle is managed; it does not create a
different authorization model.

A Control Card is the same Card aggregate without credential handles. It uses
the same catalog choices, revisions, current pointer, drift calculation,
editor, update path, and revoke lifecycle. Connection Hub owns it physically
and durably. The issuing application stores only its Card reference and any
observed revision, state, catalog, composition, or synchronization facts.

### Credential delivery and resource reach

How a caller receives a credential and how many card resources it can use are
independent properties:

| Card presentation | Credential delivery | Resource reach |
| --- | --- | --- |
| Hosted agent | Kept by the KDCube agent runtime | Multiple resources selected on one card |
| Connected app | OAuth client receives access and refresh tokens | The MCP or API resource through which it connected |
| Connected client | OAuth delivers a credential to a custom external client | Multiple resources selected on one card |
| Issued token | User creates a token and receives its bearer once | Multiple resources selected on one card |

An ordinary MCP client is a connected app. It knows the MCP endpoint it
connected to, so its card editor stays on that endpoint and the account choices
used by it. This is the established OAuth/MCP behavior.

A custom client may declare `kdcube_credential_use=multi_resource` in its
validated public OAuth metadata. The declaration changes only which catalog
the card editor presents. It grants nothing. The authorization endpoint is the
delivery route; after the user saves the card, the same credential can be
presented to any selected resource, where the normal live card, catalog, and
operation checks still run.

Agent and automation credentials represent that whole Card. Their OAuth
profile, access credential, and refresh record carry no entry resource; each
surface resolves its own exact grant from the Card's resource maps. Connector
credentials represent one MCP connection and retain its protected resource in
the profile, token exchange, refresh record, and stable identity.

## Stable Card Identity

Every Card persists an explicit `card_kind`. That field selects the identity
formula; an ID prefix, source label, selected resource, or request route never
selects it. The formulas live in
`connection_hub.delegated_credentials.cards.identity`:

| Card kind | Stable identity | Resource meaning |
| --- | --- | --- |
| `agent` | grantor + client | One hosted-agent profile can hold several compatible resources. |
| `automation` | grantor + client | One issued or OAuth-delivered automation profile can hold several compatible resources. |
| `connector` | grantor + client + entry resource | One external MCP connection represents one entry point. |
| `control` | issuer-owned credentialless coordinate | The issuing application owns its stable reference and lifecycle. |

Tenant and project scope are implied by the Card store. For a hosted agent,
the client is `kdcube-agent:<application>:<agent>`. A manual automation first
mints its client id and then derives the Card id from the grantor and that
client. Two separately registered external clients have different client ids
even when their display labels match.

Selected resources, operations, accounts, and acting scope are Card contents.
They do not change the `access_id`. Agent and automation resources must share
one compatible `identity_scope`; an incompatible addition is refused before a
revision is written. Connector Cards keep their entry resource in the key
because the connection itself is that resource-specific profile.

Create and consent first search all current Card pointers, including revoked
history, for the kind's identity tuple. One matching Card is reused. Multiple
matches are an explicit collision and fail closed; runtime code never merges
their authority. `ResidentCallerProfile` and `stable_card_access_id()` expose
the shared formulas to Projection, OAuth issuance, and Gateway code.

Pre-v7 revisions remain readable through bounded compatibility
classification. New revisions require `card_kind`. The reviewed migration
below rewrites retained history to v7 and removes runtime dependence on that
classification.

## Stored Record

### Durable record and live cache

Connection Hub bundle storage is the durable source of truth. Redis is the
TTL-managed live projection of the latest committed card revision:

```text
delegated-cards/v1/
  grantors/<subject_hash>/
    cards/<access_id>/
      revisions/card_revision_2026-08-11-14-32-07-123_00000001_8f21c47a93bd.json
      revisions/card_revision_2026-08-11-15-04-19-881_00000002_b9d06ee7124a.json
      current.json

Redis delegated-access:card:<access_id>
  latest committed live projection, or a short-lived updating/revoked marker

Redis delegated-access:cards-by-grantor:<subject_hash>
  sorted set of access_id -> expiry score for discovery
  credentialless Cards use +inf

Redis delegated-access:cards-epoch
  the Redis run_id whose projection sweep completed (see Redis rollback)

Redis delegated-access:cards-reconcile-lock
  <run_id>|<owner token> while one worker sweeps
```

The older `delegated-access:automation:<access_id>` and
`delegated-access:automation-by-grantor:<subject_hash>` records are
compatibility mirrors for pre-migration readers. They are not the durable
source or the current serving cache described in this section.

Each immutable revision contains the complete authorization decision,
`card_revision`, `catalog_version`, lifecycle state, timestamps, and non-secret
fingerprints. It never contains raw access tokens, refresh tokens, provider
credentials, or reusable session secrets. Those remain only in their bounded
live stores. The reconstructable Redis card projection likewise contains the
non-secret authority and lifecycle fields. A durable cache restore does not
reconstruct a credential handle; the OAuth, grant, or session owner resolves
that bounded state separately and returns its normal reconnect/reissue denial
when it is gone. `current.json` points to the latest committed revision and
carries its filename, integer `card_revision`, and full content hash.

### Resident bearer custody contract

The portable destination contract for a resident Card's reusable bearer keeps
non-secret handle metadata in PostgreSQL and the recoverable bearer in a
host-owned expiring secret store. `ResidentCardSecretService` creates a fresh
secret reference before committing its metadata, verifies the envelope against
the access id, Card revision, fingerprint, and expiry on every resolution, and
keeps durable cleanup obligations until verified deletion succeeds. A secret
reference is create-only and never reused for a different envelope; changing a
bound Card revision, fingerprint, or expiry requires a fresh reference.

Rotation records the outgoing reference durably in the same PostgreSQL
transaction that installs the replacement. Cleanup reads and verifies the
outgoing envelope, deletes it from host custody, and only then acknowledges the
cleanup row. A metadata-commit response loss retains the prepared expiring
secret because the commit outcome is unknown. A definitive conflict deletes
the prepared secret. A secret-create response loss also retains the bounded
record because deleting an unowned collision would be unsafe. Once a host
binds both ports, PostgreSQL plus its secret provider form the complete
resident-bearer custody boundary, while that host may use Redis for rebuildable,
non-secret Card projections.

Revision filenames follow the same timestamped, content-addressed convention
as catalog versions:

```text
card_revision_<UTC timestamp with milliseconds>_<8-digit card_revision>_<first 12 hash chars>.json
```

The timestamp and hash are mandatory. The zero-padded integer mirrors the
record field used for optimistic concurrency; it supplements rather than
replaces the common versioned-resource naming convention.

Create, edit, and revoke use a per-card shared critical section. Before writing
a new durable revision, the writer replaces the Redis live value with an
updating marker so requests cannot continue under the previous authority. It
then writes and validates the immutable revision, advances `current.json`, and
installs the new Redis projection. A cache miss loads the durable current
revision and repopulates Redis only when the card is active and unexpired. The
conditional Redis installer compares `card_revision`: delayed recovery cannot
overwrite a newer revision, an updating marker, or a revoked tombstone. The
mutation finalizer may replace the marker carrying its own mutation id, or an
ordinary projection whose revision it strictly supersedes — a read-through
refills the key when the marker's residency lapses before the commit completes.
It never displaces another mutation's marker, a revoked tombstone, or an equal
or newer revision.

Expiration deletes a credential-backed Card's Redis projection. The durable
revision remains and `expires_at` prevents cache restoration or use. A
descriptor-synchronized capability Card has no bearer, but its `expires_at` is
an inactivity lease and the same expiry gate applies. A Control Card
(`source=control`) has neither a bearer nor an expiry, so its projection remains
until an update or revocation replaces it. Revocation commits a new durable `revoked` revision
before live credential cleanup; it does not delete history. Open is Redis-first:
it reads the live-card projection by `access_id` and computes drift against
Redis-cached `active.json`. List decides membership from durable storage on
every call, because a partially lost index cannot be detected without reading
it; the grantor index accelerates discovery and carries expiry scores, using
`+inf` for expiry-free Control Cards. The index itself has no fixed seven-day expiry; expired
members are pruned by score. Every candidate is resolved through the card
cache/store: one that no longer resolves is pruned, and a durable member the
index lost is re-admitted. A missing projection is rebuilt from durable
`current.json` and the referenced timestamped revision, repopulating active,
unexpired caller Cards and active expiry-free Control Cards. Retention of
card history is an explicit administrative policy separate from authorization
TTL.

The relevant cache lifetimes have different meanings:

| Projection | Lifetime | Cache hit | Expiry or eviction |
| --- | --- | --- | --- |
| Live card | Credential-backed or descriptor-synchronized capability Card: remaining authorization or inactivity lifetime, `expires_at - now`. Expiry-free Control Card: no TTL. | Does not extend authorization. | Read the durable current revision; re-cache an active Card when its lifecycle permits use. |
| Grantor card index | No fixed whole-key TTL; expiring members are scored by `expires_at`, expiry-free Control Cards by `+inf`. | Prune expired members. | Rebuild active members from durable current revisions. |
| Updating/revoked marker | Short descriptor-owned safety or negative-cache TTL. | Deny or return temporary unavailability as appropriate. | Resolve durable current state; never infer authority from marker expiry. |

Redis outage is not a reason to bypass this serving and coordination layer with
process memory. Requests return structured unavailability. Durable-storage
outage blocks cache-miss recovery and every mutation; a governed request whose
validated card and active catalog are already hot in Redis does not perform a
durable read.

### Redis rollback

A Redis that restarts from an older snapshot does not lose Card projections, it
brings back older ones. A missing projection is safe because the reader reads
through to durable state (see "A missing projection" below). An older one is not: a present projection is served without
reading the durable pointer, so a projection one revision behind keeps serving
superseded authority, and a projection from before a revocation keeps a revoked
Card usable. On 2026-09-21 three worker Card projections came back one revision
behind their durable `current.json`, and every reconnect then failed the
mutation fence below.

Two mechanisms close this.

**Each mutation repairs its own Card first.** Inside the critical section, after
the durable expected-revision check and before the updating marker is claimed,
the writer compares the projection with the durable current revision. A `card`
projection or revoked tombstone strictly older than it is replaced with the
durable projection, or deleted when the durable revision is revoked or expired.
The marker fence compares the projection's revision, so after this step the
fence and the durable check read the same revision. Markers and equal or newer
values are never touched.

**Projections are served only after the current Redis run is swept.** Redis
reports a `run_id` that changes on every restart. `cards-epoch` records the
`run_id` whose sweep completed with no failure, and a restore from an older
snapshot brings back an older value or none. Every authority read proves the
run in the same Redis transaction as the projection it reads (`INFO`, the epoch
and the Card key in one `MULTI`), so the first read after a restart is already
refused, and no process trusts an earlier check. Until the epoch matches the
live `run_id`:

| Reader | Behaviour |
| --- | --- |
| A store-owning resolver (Connection Hub requests, the durable-backed guard, OAuth token refresh, Data Bus publish and worker, live sessions) | Runs the sweep itself, then serves. When the sweep cannot complete, or another worker of this run holds the lock, it returns `card_projection_reconciling` as unavailability (503). |
| A reader without the durable store or the grantor (the cache-only guard branch, a live-session record written before it kept the grantor) | Fails closed with `card_projection_reconciling`. It cannot sweep. |
| The agent-grant picker probe | Reads as pending. |

The sweep compares every Card projection of the tenant and project with its
durable `current.json` and repairs the ones behind, as a mutation does. A
projection with no durable Card behind it is deleted, because durable storage is
the source of truth. A revoked tombstone is deleted too: it names no grantor to
find the durable pointer by, one restored from before a re-consent would deny
the active Card, and without it readers go to the durable revision, which
denies a revoked Card. Updating markers are skipped. The epoch is
recorded only when every projection was checked without failure, and only while
the sweeping worker still owns the lock, so an interrupted or failed sweep runs
again.

The lock value carries the `run_id` and an owner token. A lock restored from an
older run is replaced, the owner renews it during a long sweep, and release and
epoch recording compare the owner token, so an owner whose lock expired cannot
delete or record over its successor.

The Connection Hub app's `card-projection-reconcile` cron runs once a minute and
is the guaranteed sweep owner, so readers without the durable store recover
without waiting for a store-owning request. They wait at most about a minute.

Legacy project Control Cards (`delegated-access:control-card:<id>`) live only
in Redis, with no durable revision a rollback could be checked against, so no
path uses them as authority: the live guard, named-service admission, Control
Card attachment and the owner's effective-authority view all resolve Control
Cards from durable revisions only. A Card still bound to a legacy one fails
closed with `control_card_unresolvable`, and the owner view reports it as
unavailable.

**A missing projection.** The sweep repairs the projections that exist; it does
not list durable Cards, so it does not recreate one that is missing. A missing
projection is restored by the reader: every Card lookup that knows the Card's
grantor carries the durable Card store, reads the durable `current.json` and
the revision it names, serves an active and unexpired Card, and restores its
projection. That restoration is the only write, and the lost projection is what
causes it. A reader without the durable store or the grantor cannot tell a
missing projection from a revoked Card, so it answers `card_projection_missing`
as unavailability (503 with `Retry-After` on the OAuth token route), never as
revoked. A revoked tombstone and a revoked or expired durable Card still deny.
Why: on 2026-09-21 the Card identity migration removed every moved Card's
projection, and the OAuth token route and the Data Bus read the absence as
"revoked", so every relay refresh was refused as `invalid_grant`.

A Redis that reports no `run_id` cannot prove its run, so it serves no Card
projection. The epoch and the lock are not durable state: losing either costs
one sweep.

### Record fields

| Field | Meaning | Public list response |
| --- | --- | --- |
| `access_id` | Stable id of this card. It selects authority; it is not the caller's identity. | yes |
| `card_kind` | Persisted identity family: `agent`, `automation`, `connector`, or `control`. It selects the stable-ID formula. | yes |
| `label`, `client_id` | Human name and delegated caller-profile identity. | yes |
| `grantor_subject`, `delegate_subject` | User who granted and integration principal that acts. | yes; list is also owner-scoped to the authenticated grantor. |
| `resource_grants` | Exact selected KDCube claims per resource. | yes |
| `resource_operations` | Exact selected outer API/MCP operation names per resource. This is the operation authority. | yes |
| `operations` | Compatibility union derived from `resource_operations`. It is not an independent authority source. | yes |
| `named_service_operations` | The card's selection: caller Cards may carry catalog-bound `"*"`; Control Cards carry only `{}` or an exact resource -> namespace -> operation map. | yes, verbatim |
| `named_services` | Materialized boundary tree derived from the descriptor and `named_service_operations`. The proc-side bridge consumes it. | no; its expansion surfaces as `effective_named_service_operations` |
| `effective_named_service_operations` | The selection expanded under the catalog version the card was saved against. Derived, never authority. | yes when the card covers any operation |
| `catalog_version`, `card_revision` | The catalog generation this card was last saved against, and its monotonic revision. | yes |
| `account_scope` | Provider -> account -> exact connected-account claims this caller may use. | yes when non-empty |
| `entry_resource` | The protected resource represented by a connector Card. Agent, automation, manual, resident-agent, and control Cards leave it empty because their authority is the Card's resource maps. | yes when non-empty |
| `identity_scope` | Which identity boundary the delegated resource uses. | yes |
| `created_at`, `expires_at`, `last_issued_at` | Lifecycle timestamps. | yes when present |
| `last_four`, `source` | Token fingerprint and card family. | yes |
| `issuer_ref`, `issuer_kind`, `issuer_label`, `manage_url` | Bounded coordinates for the application that created or presents a credentialless Card. They drive ownership UX, not authority. | yes when present |
| `composition_mode` | How this Card contributes when linked as a control: `and` (intersection, the default) or `or` (union). The field has no control effect until a caller Card links it. | yes when present |
| `properties` | Bounded, non-secret, operator-reviewed application policy stored with this Card revision. | yes when present |
| `resource_acceptance` | Per resource: the descriptor authority (`kind` `catalog` or `remote_mcp`, `provider`), the `revision` and `digest` accepted at the last save, the claims seen, and one digest per offered operation. Drift is judged against it, resource by resource. | yes when present |
| `provenance` | Non-secret lineage written by the resident-profile migration: the legacy records folded into this card, when, and any operation dropped because its one-use permit was spent. | yes when present |
| `caller_profile`, `stable_identity` | List-only: the resident profile behind an agent card and whether the card already lives under the profile's stable id. | yes for agent cards |
| `resource_offers` | List-only: owner-visible delegable resources that may join this card, each with `compatible` and a `reason` (`already_on_card`, `identity_scope_incompatible`, `admin_only`). | yes |
| `control_card` | Optional reference from a credential-backed caller Card to one credentialless Card. The caller Card remains unchanged; live admission resolves the current linked revision and composes both Cards. | yes when present |
| `project_control` | Compatibility name in older list/describe responses for the resolved linked-Control-Card view. New code treats it as a generic Control Card relationship. | yes when a link is present |
| `session_id`, `access_token`, `refresh_token` | Internal credential/revocation handles, according to source. | no |

Every authority and lifecycle field above is copied into the immutable durable
revision except `session_id`, `access_token`, and `refresh_token`. Those three
live only in dedicated TTL-managed stores, separate from the reconstructable
card-authority projection.

`to_public_dict()` removes all token material, `session_id`, and the internal
`named_services` boundary. A public card exposes the selection, never the
provider credential or the materialized enforcement tree.

The selection is retained verbatim, so a refetch preserves the difference
between the four states:

| Stored | Meaning |
| --- | --- |
| `"*"` | Caller Cards only: every named-service operation present in the referenced `catalog_version`. Operations added later are not included. A Control Card never persists this form. |
| `{}` | No named-service operation. |
| exact map | That resource -> namespace -> operation selection. |
| field absent | A record written before this encoding. Its prior set is derived from the materialized boundary. |

Card authority schema `connection_hub.delegated_card_authority.v7` requires
the explicit `card_kind` used by identity selection. V6 adds credentialless
Card coordinates, composition, and properties. V5 adds the
optional `control_card` reference. V4 adds bounded client metadata; v3 stores
outer operations as `resource_operations` and adds `resource_acceptance` and
`provenance`. Older revisions remain readable and the next successful write
uses v6. The resource qualification matters when two protected resources
expose the same operation name: selecting the operation on one resource grants
nothing on the other, and an invocation policy is keyed to the resource as
well. A v1 card with only the flat `operations` field is read with its prior
semantics by projecting that set onto each resource already selected by the
card; a v2 card reads without acceptance, and every resource of it reports
`unknown` per-resource state until the next save stamps it. A pre-resource
OAuth record is projected to
the wildcard resource `"*"`, preserving its former all-matching-resource
interpretation without making the flat union authoritative for new cards.

Connected provider credentials are stored by the delegated-to-KDCube account
system, not in these cards. `account_scope` contains ids and claims only.

Invocation policy is also not a card field. The policy registry is keyed to
the card's `access_id`, exact resource and operation, and optional connected
account. `delegated_access_list` joins current policies into each public item
as `invocation_policies` so the editor can render `Always` or `Once`, but card
revision history remains a record of delegated capability rather than a usage
counter. A manual automation mints a client id and derives its stable Card id
from that client and the grantor.

## What List And Rendering Read

### Configuration vocabulary

The two top-level collections under
`connections.delegated_credentials.oauth` answer different questions:

| Descriptor node | Question it answers | Card effect |
| --- | --- | --- |
| `capabilities[]` | **Which grant tokens exist, and may this signed-in user delegate each one?** Each row defines a `grant`, its display metadata, delegation roles/permissions, and optional connected-account requirements. | The current user's authority is evaluated against these definitions to produce `grant_options`. A capability is primarily vocabulary and a delegation rule; it is not itself an endpoint or callable operation. |
| `resources[]` | **Which protected doors exist, and what may be reached through each door?** Each row defines a resource URL/pattern, its grants, outer tools, optional named-service boundary, identity scope, and admin restriction. | The live resource catalog supplies the selectable doors and the claims, outer tools, namespaces, and inner operations beneath them. Selected claims persist in `resource_grants`. |

`resources[].grants` is the claim ceiling shown for that door. When it is not
written explicitly, the parser derives it from the grants required by the
resource's outer tools and nested named-service operations. Every grant token
used there should have a matching `capabilities[]` definition so Connection
Hub can decide whether the current user may delegate it and provide its label.

For a provider-backed named-service operation, the operation row may name both
the KDCube door grant and the provider claim it needs. Connection Hub presents
those requirements in separate scopes. The door grant is stored in
`resource_grants`; the provider claim is selected on a specific connected
account in `account_scope`. Admission treats that exact account binding as
effective satisfaction of the provider-claim prerequisite, without copying the
provider claim into `resource_grants`. It does not add an operation to
`named_service_operations`; the user selects that operation independently.

`capabilities[].tools` remains a compatibility/fallback source of outer tool
metadata when no resource-specific tool catalog matches; current Connection
Hub descriptors define protected operations under `resources[].tools`.

There are also two different operation layers:

| Layer | Canonical descriptor shape | Example | List/card representation |
| --- | --- | --- | --- |
| Outer surface operation | `resources[].tools.<name>` | `named_services_schema` | The list response calls these `resources[].operations`; the card stores the exact resource -> operation selection in `resource_operations` and returns its flat `operations` union for compatibility. They are API/MCP entry operations at the protected resource. The parser also accepts `operations`, `allowed_tools`, and `actions` as input aliases, but `tools` is the canonical descriptor spelling. |
| Inner named-service operation | `resources[].named_services.namespaces.<namespace>.tools.<tool>.operation`, or the nested `<tool>.operations.<operation>` map | namespace `linkedin`, operation `object.schema` or `object.action.publish_post` | The exact user selection is stored in `named_service_operations`; the derived bridge policy is stored internally in `named_services`. These are the ontologic operations inside the named-services door. |

The word `capabilities` can also occur as a named-service tool key, for
example `tools.capabilities.operation: provider.capabilities`. That is merely
an inner callable operation. It is unrelated to the top-level
`oauth.capabilities[]` grant vocabulary.

### Application operations and delegated role

The Card editor also projects the current KDCube bundle catalog as
**Application APIs**. These rows are authority, not descriptive inventory.
The Card stores two independent choices on the wildcard application resource:

```text
resource_grants["*"]       selected delegated platform role
                            used as the default projection, for example
                            kdcube:role:registered

resource_operations["*"]   selected canonical application operations
                            for example
                            urn:kdcube:application-operation:reports%401-0:report.read

properties["kdcube.application_operations"]
                            the same default role plus optional exact-operation
                            role overrides
```

The canonical reference is
`urn:kdcube:application-operation:<application-id>:<operation-id>`, with both
components percent-encoded. Application scope prevents the same alias in two
apps from colliding. KDCube derives a distinct default operation id from an
API's route, HTTP method, and alias. An app may instead declare one explicit
`operation_id` on several transport exposures when they perform the same
governed action. Connection Hub stores the resulting app-scoped reference;
REST, Data Bus, or another adapter does not add its own permission identity.

At runtime, the application declaration and Card both narrow the call:

```text
selected app-scoped operation
  AND exact operation override OR selected default platform role
  AND current application visibility and auth policy
  = effective application call
```

The selected role is the delegate's execution role. The resource descriptor's
role is a maximum, so a single `kdcube:role:super-admin` ceiling admits lower
registered, paid, and privileged projections. It does not add the application
resource to a Card that did not select that resource. A super-admin may choose
`kdcube:role:registered`; the resulting call is registered and does not inherit
the grantor's unselected admin role. One selected operation may override that
default with a stronger role when both the grantor and resource ceiling permit
it. The stronger role exists only for that invocation and cannot raise a
sibling operation.

An explicit selection is marked in Card `properties` so a reviewed empty list
continues to mean no application operations:

```json
{
  "kdcube.application_operations": {
    "schema": "kdcube.application_operations.v2",
    "mode": "selected",
    "default_role": "kdcube:role:registered",
    "operation_roles": {
      "urn:kdcube:application-operation:reports%401-0:report.delete":
        "kdcube:role:super-admin"
    }
  }
}
```

Version 1 marked only the reviewed operation selection and infers one default
from the historical resource grants. Version 2 makes the default and overrides
explicit. Cards created before either marker may already contain an empty
wildcard operation row for another purpose. They keep their previous behavior
until the owner changes an Application API choice. New Cards that include the
wildcard application resource carry version 2 from creation. OAuth consent,
manual Card creation, resident-card updates, Card edits, refresh rotation, and
Control Card creation all preserve it. This explicit migration boundary avoids
turning an old empty row into an accidental deny-all policy during upgrade.

When a pre-marker caller is linked to an exact Control Card with `and`, the
caller's historical platform role remains a ceiling and the Control Card's
reviewed operation list supplies the finite operation set. An empty reviewed
Control Card list therefore grants no application operations. With `or`, two
Cards that both contribute the application resource carry explicit policies so
their operation-to-role mappings can be combined without introducing
unbounded historical authority. An application-policy property on a Card that
does not carry the application resource is metadata only and is omitted from
the effective Card.

### Live-services source fusion

The card editor does not obtain one preassembled "live services" object from
one store. Connection Hub joins configured ceilings, current user authority,
live provider requirements, connected-account state, and any stored card
selection:

```text
CONFIGURED DELEGATION CEILING                         CURRENT USER FACTS
connections.delegated_credentials.oauth
|
+-- capabilities[]                                   signed-in roles/permissions
|     grant + label                                             |
|     delegable_roles/permissions                               v
|     optional connected_accounts -----------> PlatformAuthorityInventoryProvider
|                                                          |
|                                                          +--> grant_options[]
|                                                               grants this user
|                                                               may delegate now
|
+-- resources[] ------------------------------------------> resources[] (doors)
      resource URL/pattern
      label / identity_scope / admin_only
      grants[] -------------------------------------------> claim rows
      tools{} --------------------------------------------> outer operation rows
        named_services_schema                                 |
          grants: [named_services:use]                        +--> card.resource_operations
                                                                 [resource] -> operation[]
                                                                 + flat card.operations union
                                                                   for compatibility
      named_services
        namespaces
          linkedin
            tools
              schema.operation: object.schema --------+
              call.operations:                        |
                object.action.publish_post -----------+--> NamedServiceBoundaryCatalog
                                                            |
                                                            +--> namespace/operation rows
                                                                 |
                                                                 +--> selected exact subset
                                                                      card.named_service_operations
                                                                      card.named_services
                                                                      (derived internal tree)

LIVE NAMED-SERVICE PROVIDER DISCOVERY
provider spec.metadata.connected_accounts ----------------> requirement annotations
  provider_id / connector_app_id / claims                    for descriptor namespaces
  claims_by_operation                                        only; adds no operation
                                                             and grants no authority

DELEGATED-TO-KDCUBE CONFIG + ACCOUNT STORE
provider and connector-app labels -------------------------> account-picker vocabulary
signed-in user's connected account rows -------------------> accounts, status, held claims
                                                                 |
                                                                 +--> selected exact binding
                                                                      card.account_scope

EXISTING CARD items[] --------------------------------------> seeds edit selections
PENDING DEMAND / DEEP LINK ---------------------------------> focuses requested rows only
                                                             (never grants by itself)
```

The namespace and operation tree is descriptor-owned. Specifically,
`NamedServiceBoundaryCatalog` projects only
`resources[].named_services.namespaces`; provider discovery does not invent a
namespace, add an operation, or make an operation callable. It contributes
the live provider's `metadata.connected_accounts` requirements, including
operation-specific provider claims such as `claims_by_operation`, so the UI
can explain which connected account is needed for a selected operation.

Those requirements are not impossible to express in configuration. They are
currently provider-owned runtime metadata because they describe what the
registered provider implementation needs, and sourcing them there avoids
copying the same provider contract into every delegated-resource descriptor.
This is distinct from `capabilities[].connected_accounts`, which is a
descriptor-owned mapping from a door grant such as `mail:read` to the provider
claim that satisfies that grant. If provider discovery is unavailable, the
descriptor namespace/operation rows still render, but the account-requirement
guidance is absent; no authority is broadened.

The delegated-to-KDCube catalog then contributes provider and connector-app
labels plus the signed-in user's current account rows, held claims, and
connection status. It powers account selection and connect/reconnect/approve
guidance. Provider credentials never enter the card or its list response.

### Projection into a saved card

At create or edit time, the server combines the selections with the current
catalogs in this order:

```text
selected door + claims
  -> resource exists in current resources[]
  -> every claim belongs to that door
  -> every claim is currently delegable by this user via capabilities[]

selected namespace operations
  -> namespace and operation exist in that door's NamedServiceBoundaryCatalog
  -> operation membership is selected explicitly; neither resource_grants nor
     account_scope imply an operation
  -> KDCube door grants are present in the selected resource claims
  -> provider-backed grants may instead be present on an exact account_scope
     binding identified by the namespace's connected-account requirements;
     they are effective for this operation without being copied into
     resource_grants

selected accounts
  -> normalize explicit provider -> account -> provider-claim binding
  -> live broker later verifies the account and held claim on provider use

successful projection
  -> resource_grants             exact selected door claims
  -> operations                  eligible outer surface tool names
  -> named_service_operations    exact selected inner operations
  -> named_services              narrowed descriptor tree for the bridge
  -> account_scope               exact connected-account binding
```

Resource, grant, and named-operation membership are validated during this
projection. `account_scope` is shape-normalized here; current account
existence and provider-claim possession are enforced by the connected-account
broker when the provider operation is attempted.

The manual create form sends an explicit namespace map for every selected
resource, including `{}` when no named-service operation was checked. Its UI
therefore starts named-service access closed. At the service API level,
omitting `named_service_operations` still has legacy/full-policy semantics;
that distinction, and the current failure to persist explicit-empty durably,
are described under edit semantics below.

`delegated_access_list` is a composite response. Its three top-level parts
come from different sources:

| Response field | Source | Purpose |
| --- | --- | --- |
| `items` | Persisted `AutomationAccessRecord` rows for the authenticated grantor. | What the user already granted. |
| `grant_options` | Live `PlatformAuthorityInventoryProvider` over configured capabilities and the current user's delegable authority. | Claims the user may grant now, with labels and descriptions. |
| `resources` | Live `connections.delegated_credentials.oauth.resources` descriptor parsed by `OAuthDelegatedClientConfig`. | Resources, top-level tools, grants, admin restrictions, and named-service catalogs available now. |

For each resource with a `named_services` block, Connection Hub projects the
current namespace and operation tree through `NamedServiceBoundaryCatalog`.
It enriches namespace rows with connected-account prerequisites from live
named-service provider discovery. The separate delegated-to-KDCube catalog
supplies the user's currently connected accounts and their claims to the
account picker.

This produces two deliberately different views:

### Read-only card

- Doors and access claims come from stored `resource_grants`.
- The Services row comes from stored `named_service_operations`.
- Account bindings come from stored `account_scope`.
- Current catalogs provide friendly labels when a match still exists.
- A missing connected account is displayed as a stale binding; no provider
  token is fetched merely to render the card.

### Create or edit form

- Available resources, claims, namespaces, and operations come from the live
  descriptor-backed catalogs.
- Check states begin from the stored card selection during edit.
- Claim editing unions the card's stored claims with the current resource claim
  catalog, so a removed stored claim remains visible and can be unchecked.
- Named-service operation rows come from the live catalog, ticked from the
  card's coverage (`effective_named_service_operations`) intersected with what
  the catalog still offers. An operation the catalog added after the card was
  saved appears unchecked.
- The picker is offered for every card family.
- Account rows come from current connected accounts. A disconnected account is
  visible in read-only mode but is not seeded into the edit picker.

The direct answer to "where does the Services list come from?" is therefore:

```text
read-only Services row    -> card.effective_named_service_operations (coverage)
create/edit Services rows -> resources[].named_services (live descriptor catalog)
edit check states         -> coverage ∩ what the catalog still offers
provider prerequisites    -> live named-service discovery metadata
account choices           -> live delegated-to-KDCube account catalog
```

## Creation Lifecycle

### Manual automation

```text
authenticated user opens create form
  -> list returns live resources and grant choices
  -> user selects resource grants, namespace operations, and account scope
  -> create validates every selection against current config and authority
  -> selected named-service operations narrow the descriptor policy
  -> bearer and access-grant binding are minted with registry_access_id
  -> card record is stored and indexed
  -> raw bearer is returned once
```

### Hosted agent

```text
agent attempts a governed operation
  -> denial names the deterministic kdcube-agent:<app>:<agent> client, the
     namespace and the operation it was refused
  -> user approves the demand in Connection Hub, with that operation pre-selected
  -> create_access addresses the profile's ONE stable card (grantor + app + agent);
     legacy resource-dependent records fold into it first
  -> approval merges exactly the approved authority; a new resource joins the
     same card
  -> explicit edit uses replace semantics
  -> reusable agent bearer and card are stored server-side
```

A descriptor-controlled resident agent uses the same stable profile Card ID
with a different credential contract. Descriptor synchronization creates a
Card whose `kdcube.agent_capability_selection` property stores the user's
starting selection and whose linked Control Card stores the descriptor-owned
ceiling. This Card carries no bearer or refresh token. Its seven-day
`expires_at` is an inactivity lease chosen for the capability projection, not
an access-token lifetime. Every agent message synchronizes before projecting
tools. While the Card remains current, an unchanged sync writes nothing. After
the lease lapses, the Card grants nothing; the next message writes a renewed
revision under the same `access_id`, preserves the selected capability base,
and only then projects tools. The owner therefore keeps one legible Card and
selection across revisions without abandoned authority remaining live forever.

When no resident selection exists, the application supplies the descriptor
default as the first positive Agent Card selection. Once the Card exists,
ordinary descriptor synchronization preserves that selection: a capability
added to the descriptor appears inside the Control Card ceiling but remains
outside the resident Card until the user selects it. Connection Hub edits this
base through a dedicated revision-checked selection update, bounded by the
current linked Control Card. Values outside that ceiling cannot be added.

The owner-facing surfaces preserve the distinction. The resident Agent Card
is the editable starting selection. Its descriptor Control Card is a read-only
view of the synchronized ceiling and its labels and descriptions, rather than
an empty generic resource editor. A capability picker can open the resident
Card directly with `tab=delegated_by_kdcube&access_id=<resident-card-id>`; the
Connection Hub surface resolves and edits that exact Card.

The focused consent view projects that one denied invocation into an
always-expanded review. It keeps four selections separate:

1. the exact outer operation, plus the named-service namespace and operation
   when the request enters that inner subsystem;
2. whether the exact outer operation is allowed once or always;
3. the KDCube door grants required by that operation;
4. the required provider claim on a specific connected account.

The full service catalog remains an optional editor beneath this focused
review. The requested operation and its relevant KDCube grants are selected as
a proposal. The relevant provider is expanded and every matching connected
account is visible. When the denial identifies an exact account and claim, only
that account/claim pair is proposed; a second account is never inferred or
selected. When no account is identified, no new account binding is proposed and
the user chooses the account.

Every checked value names its authority state. `Already granted` means it is in
the current card. `Pending - not granted yet` means the current form proposes
adding it. Pressing `Allow once` or `Allow always` persists the selected
operation, initial invocation policy, door grants, and account binding as one
fail-closed change. An operation demand cannot be submitted while its operation
or a required door/account selection is absent. Provider claims remain
prerequisites only: selecting or already holding one never selects a tool
operation.

### OAuth/MCP client

```text
external client completes OAuth consent
  -> consent submits its contract version, the catalog version it was rendered
     from, and the operation selection
  -> authorization code carries that selection
  -> token endpoint issues access/refresh material
  -> record_oauth_grant writes one card per grantor + client + resource, born
     with the submitted selection and the catalog version
  -> refresh rotation updates token handles, expiry and last_issued_at only
  -> user edits or revokes the same visible card later
```

A reconnecting dynamic client may receive a new client id. Connection Hub can
supersede a matching older card, carry its account binding forward, and revoke
the old token handles.

### Consent view model

Connection Hub builds one view model per authorize request and hands it to
whichever renderer serves the page — the built-in one or a bundle-hosted
`consent_ui`. A custom renderer changes presentation, never the authorization
contract.

```text
consent_contract.version
catalog_version
platform_grants
tools                       (outer operations)
named_service_operations    (namespace, operation, required claims, held)
connected_accounts
seeded_account_scope
seeded_named_service_operations
request / oauth_request     (client and PKCE metadata)
```

`seeded_named_service_operations` is what the client's card covers today, read
from its materialized boundary — so a wildcard card seeds the expansion it is
pinned to, not the current catalog. Each `named_service_operations` row carries
`held` for the same fact per row. A submission REPLACES the card's selection,
so a renderer that cannot tell a held operation from a newly offered one cannot
offer an informed choice: it would present an empty picker whose quiet
submission removes the whole grant. The built-in page checks held operations and
lists the rest under "added since this client was last approved".

A renderer returns the contract version it implements alongside its HTML. An
absent or mismatched version is refused with `consent_ui_contract_mismatch`
before the page is shown, so a page that never rendered a dimension cannot
report a choice about it.

The submitted form carries `consent_contract_version`,
`expected_catalog_version`, and the operation selection:

| Submitted | Result |
| --- | --- |
| Every offered operation selected | `"*"`, bound to the catalog version shown on the page. |
| Some operations selected | That exact selection, re-validated against the catalog. |
| None selected | `{}`. On a first consent the connection is created without named-service access; on a re-consent this REMOVES every operation the card held. Both can be widened later in Connection Hub. |
| `consent_contract_version` absent | No authorization code and no card. |
| `expected_catalog_version` no longer active | `consent_catalog_changed`; authorization restarts rather than reinterpreting the selection. |

## Edit Semantics

The card's selected authority is changed in place. No new manual token is
issued, and a pointer-backed caller observes the change on its next request.

Authority is changed by the grantor, under their KDCube identity, wherever the
edit was initiated from. A caller authenticated as the delegate
(`integration:<client>:<grantor>`) is refused with
`delegated_access_requires_grantor`.

### Any card: `delegated_access_update`

One source-neutral edit serves all three families. The card keeps its
`access_id` and its credential material — an agent's reusable bearer, an OAuth
client's token handles, a manual card's client-side token — and the new
authority applies on the caller's next request, with no re-mint and no
re-authorization.

`delegated_access_update` validates and rewrites the record with this contract:

| Input | Meaning |
| --- | --- |
| `resource_grants` | Required full replacement. No remaining grant means revoke, not update. |
| `named_service_operations` omitted | Preserve the stored narrowing. |
| `named_service_operations: {}` | Disable all named-service operations for the selected resources. |
| `named_service_operations: "*"` | Caller Cards: select every named-service operation in the current catalog and bind that choice to the saved catalog version. Control Card updates reject this input and require the exact map shown to the operator. |
| `named_service_operations` with content | Replace with that exact resource -> namespace -> operation selection. |
| `account_scope` omitted | Preserve the current account binding. |
| `account_scope: {}` | Bind no provider account; provider-backed use is default-closed. |
| `account_scope` with content | Replace with the exact provider -> account -> claim selection. |
| `label` | Rename without changing omitted dimensions. |
| `accepted_operations` | Per resource, the selected operations whose CHANGED descriptor the grantor reviewed and accepts with this save. Every other changed selected operation keeps the digest the card accepted before and stays suspended. Omitted accepts nothing. |

The update recomputes outer operations and the materialized `named_services`
tree from the active catalog available to Connection Hub. For a stored `"*"`,
that materialized tree contains the exact operation set present at Save time;
governed execution can therefore intersect the card with current
`active.json.connections` without loading catalog history.

For credential-backed caller Cards, the persisted
`named_service_operations` field carries the complete policy:

| Stored value | Meaning |
| --- | --- |
| `"*"` | Permit every named-service operation reachable through the card's selected resources in its referenced `catalog_version`. Later additions are not included. |
| `{}` | Permit no named-service operation. |
| Resource/namespace/operation map | Permit exactly the named entries. |

The list response retains both `"*"` and `{}`. New caller records always persist
one of the three forms. Control Cards use the stricter exact-snapshot contract
below and persist only an exact map or `{}`.

Omission means different things per direction:

| Direction | Omitted `named_service_operations` |
| --- | --- |
| create | `{}`. Claims do not select operations; `"*"` is stored only when the user chose every operation the current catalog offers. |
| update | Preserve the card's own policy. A preserved `"*"` is written out as the exact set it already meant, so an unrelated save cannot re-pin it to a newer catalog. |

A **pre-migration card record** is an ordinary card written before
`catalog_version`, `card_revision`, and this encoding were introduced. Its prior
set is derived from the persisted materialized `named_services` boundary; GET
does not mutate it. Where that derivation would change authority silently, the
server reports `migration_confirmation_required` and refuses a save that carries
no explicit selection:

| Condition | Why it is not derivable |
| --- | --- |
| The boundary names no operation. | Deriving yields `{}`, indistinguishable from a deliberate empty selection. |
| The derived set is empty against the active catalog. | The migration would revoke without an operator decision. |
| The card holds more than one named-service resource. | The boundary is stored as one merged tree; per-resource attribution is not recoverable. |

When an existing credential-backed `"*"` card is saved after catalog drift without an explicit
new wildcard choice, the backend expands the wildcard against the saved
catalog version and persists the surviving exact set before advancing
`catalog_version`. An explicitly submitted `"*"` selects all operations shown
from the current catalog and binds that wildcard to the new version.

### Agent card

An agent card has a second entrance: the consent demand. It MERGES exactly the
capability that was refused — the namespace and operation the demand names, with
the claims and account binding they need — into the card's existing selection.
The card's own side is frozen into what it already meant before the merge, so a
one-click approval never re-pins a `"*"` to a newer catalog.

`delegated_agent_grant_create` merges by default, so separate approved demands
accumulate. With `replace: true` the submitted resource claims replace that
resource's selection; omitted `account_scope` and `named_service_operations`
preserve their stored values, and explicit empty maps clear those dimensions.

When the same request also chooses `once` or `always`, the operation merge and
policy write form one cross-registry transaction. A prepared policy marker
closes invocation first, the card merge runs second, and the policy commits
last. If the final commit is interrupted, calls remain denied until the same
change id is retried. This avoids a visible card grant with an absent or older
usage policy.

### OAuth card

`extend_client_access` merges a one-click extension or replaces one resource's
claims, and merges or replaces `account_scope`. It resolves authority through
the same path as every other save, so it also rewrites
`named_service_operations`, the materialized boundary, and the catalog stamp.

Re-authorization is not required to change an OAuth card: the edit rewrites the
live card and the existing pointer-backed bearer sees the new authority on its
next call. A refresh rotation updates credential handles, expiry, and
`last_issued_at` only — it neither restores an older operation selection nor
widens one.

#### The client's one door

An MCP client such as Claude Code, Hermes or OpenClaw is configured with one
URL and connects to that protected resource only; it cannot learn another
door exists, let alone call it. The card records that resource as
`entry_resource`, and the editor offers it only what consent at that door
offers: the door's own selection rows (a proxy door's connectors), computed
by `_reachable_through_door` from the same `resource_selection_rows` the
consent screen uses. Every other catalog row comes back in `resource_offers`
with reason `outside_client_door`, and the editor hides those instead of
listing doors the client will never call, naming the door once. A door
without `resource_selection` reaches nothing else. Manual and resident cards
pass no reachable set and keep every delegable door, because their callers
address doors by configuration. The read-only card leads with the entry door,
badged `client door`, and shows the rest as served through it.

## Multi-Resource Cards

One card carries several resources under one identity. The mutation contract:

| Mutation | Behavior |
| --- | --- |
| Create with several resources | One card; every resource carries its own claims, exact operations, and acceptance. |
| Incremental consent (`delegated_agent_grant_create`, `create_access(client_id=...)`) | Merges exactly one resource/operation into the profile's stable card. A resource the card did not hold is added with its own operations; the other resources are untouched. |
| Ordinary edit (`delegated_access_update`) | Replaces the complete submitted contents. A resource absent from the submission is removed. |
| Removing one resource | The other resources, their invocation policies, the account binding, the credential binding, `access_id`, `created_at`, and provenance stay intact. Policies keyed to the removed resource are left in the policy registry; they grant nothing while the card does not hold the operation. |
| Removing the final resource | Refused (`delegated_access_requires_resource_grants`). Ending a card is a revoke, decided explicitly. The editor offers Revoke instead of Save. |
| Incompatible identity scope | Refused before any write, naming the scopes. No second resident card is created. |
| Re-adding a removed resource or operation | Lands on the same card. A consumed one-use policy for that operation is still consumed: the policy registry is keyed by card, resource, and operation, so a spent permit never comes back to life through a card edit, and a revoked card is not reactivated. |

Equal operation names on different resources remain independent throughout:
`search` on memories and `search` on tasks are two operations, two acceptances,
and two policies.

## Migration Of Resource-Dependent Resident Records

Records written under the earlier resource-dependent agent id are folded into
the profile's stable card. The fold runs before a grant lands on the stable
card (`create_access` with a client id) and can be run on its own through
`AutomationAccessService.migrate_resident_profile`. Its rules are fail-closed:

- candidates are the grantor's own active agent cards with this exact client
  id, excluding the target; no other profile's record can be a candidate;
- source cards must agree on acting identity scope, otherwise
  `identity_scope_conflict` and nothing changes;
- two records holding the same resource must agree on its claims and
  operations, otherwise `resident_profile_migration_conflict`
  (`resource_selection_conflict`) and nothing changes;
- non-empty account bindings must be identical across every record folded,
  including the target; a binding one card made for its resource is not
  extended to another card's resource (`account_scope_conflict`);
- the folded card expires when the earliest of its sources would;
- invocation policies move with their operation: `always` and an unconsumed
  `once` are re-declared on the stable card before the card is written, a
  consumed `once` drops the operation from the folded card so a spent permit
  cannot revive, and a target policy of a different mode is a conflict. Without
  a configured policy service the fold refuses (`invocation_policies_unverifiable`);
- the target card is persisted with a fresh reusable bearer bound to the stable
  id, then the legacy cards are revoked, which invalidates their bearers. The
  legacy resource-specific resolver remains compatible before the fold; the
  unified Gateway runtime uses only the stable exact id and therefore waits for
  migration;
- replay is a no-op: a second pass finds no candidates, and a concurrent
  worker loses the card's revision precondition and skips;
- `provenance.migrated_from` on the stable card names the source records and
  their revisions, `migrated_at` the moment, and `dropped_consumed_once` any
  operation the fold left out. No credential value appears anywhere.

A conflict is reported to the caller with the candidate records and a recovery
action: review those cards in Connection Hub, revoke or edit the ones that
should not carry over, then grant again.

## Control Cards And Effective Authority

A credential-backed caller Card may link at most one credentialless Card. Both
are ordinary Connection Hub Cards. The link, not a separate Card type, gives
the credentialless Card its control role:

For a descriptor-controlled resident agent, capability composition has one
positive projection. The Control Card contributes the descriptor-owned ceiling
and the resident Agent Card contributes the user's selection. Connection Hub
derives conventional admission properties, including
`kdcube.conversation_targets`, from that effective projection. Those properties
are transport views for established guards, not additional selections and not
independent sources of authority.

```text
presented credential
        |
        v
caller Card -- optional control_card link --> current linked Card
        |                                      |
        +---------- configured AND or OR ------+
                           |
                           v
                     active catalog
                           |
                           v
                  effective caller Card
                           |
                           v
                  requested operation
```

No link means the caller Card is used unchanged. A present link is an explicit
authorization dependency: a missing, unreadable, updating, revoked, or
mismatched linked Card fails closed and is never treated as no link.

`and` intersects resource grants, outer operations, named-service operations,
connected accounts, and account claims. `or` unions the two Card selections.
The active catalog is applied after either composition, so an option removed
by the deployment cannot be restored by either Card. The default is `and`.
Both Cards must use the same `identity_scope`, preserving the ordinary Card
invariant that all effective resources act through one identity boundary. A
mismatch is refused when the link is created and fails closed if a later Card
revision introduces it.

The linked Card has no credential and cannot be presented to a service. It has
no expiry. It stops contributing only when the owner revokes it or removes the
link. Unlinking restores the unchanged caller Card; it never resurrects a
caller Card that was already revoked.

Control Card choices come from the current catalog, but the saved authority is
an **exact snapshot**. On first creation, **Select all** means enumerate every
entry selected from that active catalog generation. It never means "this entry
and everything published under it later." The immutable revision stores those
exact values and the basis `catalog_version`; it stores no authority wildcard
in `resource_grants`, `resource_operations`, `named_service_operations`, or
`account_scope`. The wildcard application resource key `"*"` remains its
stable resource identifier, while its role and operation lists are exact and
its `kdcube.application_operations` policy is explicitly default-closed.

The Card records this contract in bounded properties:

```json
{
  "connection_hub.control_snapshot": {
    "schema": "connection_hub.control_snapshot.v1",
    "mode": "exact",
    "state": "exact",
    "basis_catalog_version": "delegated_catalog_<timestamp>_<hash>",
    "origin": "created"
  }
}
```

A caller Card may supply the initially checked values at creation, including
authority-defining bounded properties such as the application-operation policy
marker. Explicit values supplied by the issuing application override equal
seed-property keys. The seed is not retained as a maximum, but the exact
selection created from it is the Control Card's first catalog snapshot. Later
saves may select any option the current catalog and grantor allow and write a
new exact revision with the current catalog as its basis.

Resources and operations published after the basis version are listed as
newly available with `selected: false`. They remain outside the Control Card
until the owner selects them and saves. Selecting every currently displayed
entry performs one bounded inclusion for that catalog generation; it does not
install a future wildcard. The editor shows **Exact catalog snapshot**, the
basis and current versions, catalog drift, and any unresolved migration
dimension, so an exact all-selected state is visibly different from a legacy
open-ended state.

Runtime composition first verifies the exact marker and rejects every
authority wildcard. AND and OR preview applies the same check and produces an
empty effective authority for an unmarked or wildcard control. This keeps the
preview and the admission decision on the same stored sets.

### Legacy Control Card freeze

The owner-facing read of an unmarked Control Card freezes it once into a new
immutable revision. Migration reads the first unambiguous immutable revision,
verifies its content-addressed filename, and materializes any historical
wildcard only from that revision's accepted catalog evidence and materialized
boundary. It never expands against the active catalog because doing so would
silently ratify capabilities added after creation. Every later-added capability
therefore remains excluded until the owner explicitly selects it in the
current catalog.

If the first revision lacks evidence for one authority dimension, migration
does not guess. That dimension becomes an exact empty selection and metadata
state becomes `review_required`, naming what the owner must inspect. A confirmed
absence of usable history creates an editable deny-all snapshot. Ambiguous,
corrupt, or unreadable history refuses migration instead of choosing one
candidate; runtime remains closed until storage is repaired or the operator
performs an explicit recovery.

Connection Hub owns creation, immutable revisions, the current pointer,
catalog drift, editing, update, revoke, linking, and live composition. The
application that requested the Card supplies issuer coordinates and may store
only its Card reference plus observed status facts. Application-specific
policy belongs in the Card's bounded `properties`; for example, Problem Board
uses `properties.coordination.version_control.{model,reason}`.

The exact Card editor uses the ordinary resource, operation, named-service,
and per-account controls. It also exposes `and`/`or`, and places **Revoke**
beside **Save** and **Cancel**. The Card need not be copied into an
application-specific editor. The owner-scoped operations are
`control_card_create`, `control_card_get`, `control_card_attach`,
`control_card_detach`, and `control_card_revoke`; edits use the ordinary
`delegated_access_update` path. The older project-control operation aliases
remain compatibility adapters for already-staged callers.

## Runtime Enforcement Lifecycle

The cross-surface flow from a card and active catalog through managed REST/MCP,
plain account-backed tools, named-service admission, direct dispatch, and Data
Bus relay is owned by
[Delegated Authority And Admission](./delegated-authority-and-admission.md).
This section continues with the card-specific pointer resolution performed by
the managed guard.

Pointer-backed credentials carry `registry_access_id` in the access-grant
binding. The managed guard treats that id as a pointer, not as authority by
itself. Its concrete delegated path is:

```text
request bearer
  -> authenticate bundle/OAuth token
  -> load access-grant binding
  -> read registry_access_id
  -> resolve card from delegated-access store
  -> verify expected client, grantor, delegate, expiry, and record shape
  -> when control_card is present, resolve its current live projection
  -> compose caller and linked Card with the linked Card's and/or mode
  -> intersect the composed authority with the active catalog
  -> copy current card facts into the request-local grant
       resource_grants
       flattened grants/scopes
       operations
       account_scope
       named_services materialized boundary, always set
  -> enforce outer resource/tool gate
  -> enforce named-service namespace/operation boundary
  -> resolve a permitted connected account and its claims, when required
  -> call provider
```

Missing, expired, malformed, unavailable, or identity-mismatched pointer
authority fails closed. Revocation commits a durable revoked revision, replaces
the live serving state, and invalidates the source-specific session/token
records. An already-issued bearer therefore sees revoked current state on its
next invocation. An invocation that already received its singular admission
decision completes under that decision.

If durable revocation commits but publishing its tombstone or index state
fails, source-specific session/token invalidation still runs. The operation
then returns a retryable `503 delegated_card_serving_state_unavailable` naming
the committed card; it does not report that revocation failed or leave the
credential cleanup behind the failed serving-state write.

Legacy bindings without `registry_access_id` retain embedded-snapshot semantics
for the card's own facts; the active-catalog intersection still applies to them.

The named-service boundary is always carried, including when it is empty. An
absent boundary would leave the descriptor as the only ceiling, so a card that
materialized nothing permits nothing.

## Descriptor And Catalog Drift

Stored consent and current deployment policy are different facts:

```text
stored selection       = what the user approved
current catalog        = what the deployment offers now
effective authority    = never more than both permit
```

### Behavior on a descriptor change

| Descriptor change | Card/list behavior | Runtime behavior |
| --- | --- | --- |
| A claim or operation is added | It appears in the live create/edit catalog, unchecked. Existing cards do not select it automatically. | Existing selections do not gain it; a card holding `"*"` is bound to the catalog version it was saved against. |
| A stored top-level claim is removed | Drift marks it removed; the claim editor still renders it so it can be unticked, and Save prunes it. | The governed call intersects the card with the active catalog and denies it. |
| A stored named-service operation is removed | Drift marks it removed and states that it is already ineffective. | Denied immediately, even while provider code still implements the operation. |
| A whole stored resource is removed | Drift marks the resource removed; Save prunes it, and a card left with no authority is revoked rather than kept. | Denied immediately. |

Two mechanisms hold that together:

1. **Enforcement clamps immediately.** A governed call reads complete cached
   `active.json` from Redis and intersects its embedded `connections` mapping
   with the card. A Redis TTL miss reads through to the committed durable
   `active.json` and repopulates Redis with the configured fixed TTL. It returns
   `503 temporarily_unavailable` when that document cannot be obtained or its
   `content_hash` does not match its embedded mapping.
2. **The editor explains and repairs stored drift.** List and edit return a
   server-computed drift projection. The UI shows **Service access changed since
   this grant was last saved**, with details: removed selections are already
   ineffective and are removed on save; newly available choices stay unchecked
   until explicitly granted.

Save reconciles in this order:

```text
stored selection
  -> retain stale entries long enough to explain them
  -> prune entries absent from the current catalog
  -> validate every remaining selection strictly
  -> persist the reconciled explicit selection
  -> rebuild the materialized boundary
  -> stamp the active catalog_version
  -> clear the warning
```

GET must not silently rewrite the user's record. Enforcement may narrow
immediately, while the stale stored value remains available as evidence until
the user saves or revokes.

### Catalog ownership and versioning

Connection Hub owns one central history of the delegable catalog. A card never
stores a catalog copy. It stores only `catalog_version`, referring to the
immutable catalog version active when that card was created or last saved.

The catalog source is the existing effective, non-secret `connections` mapping
from Connection Hub bundle props:

```python
connections = copy.deepcopy(entrypoint.bundle_props.get("connections") or {})
```

Each immutable version document stores that parsed mapping under its
`connections` field in the existing shape. The surrounding object adds only
`version`, `content_hash`, and `created_at`. It is not normalized, flattened,
or enriched. Existing configuration readers remain responsible for
interpreting OAuth capabilities and resources, outer operations, named-service
boundaries, and delegated-to-KDCube configuration. Current provider discovery,
user accounts, roles, held provider claims, and connection status remain live
rendering and enforcement inputs; they are not copied into catalog history.

The shared store contains immutable versions and one self-contained active
document:

```text
delegated-catalog/v1/
  versions/<version>.json   immutable catalog document
  active.json               complete active catalog document
```

Both document forms contain:

```json
{
  "version": "delegated_catalog_2026-08-11-10-30-00-123_d4e5f6a7b8c9",
  "content_hash": "<full lowercase SHA-256>",
  "created_at": "2026-08-11T10:30:00.123Z",
  "connections": {"...": "exact effective props mapping"}
}
```

Canonical JSON is used only to calculate `content_hash` from `connections` for
change detection, deduplication, and integrity validation. It does not create a
second catalog shape. Publication copies current `connections`, enters a shared
critical section, rereads and rehashes the mapping inside that section, writes
the immutable version, and atomically replaces complete `active.json`. The
shared-operation signature is written from that same in-section hash, not from
the snapshot captured before the lock. The version name combines a sortable
UTC timestamp with a content-hash suffix.

Durable storage and request serving have one clear boundary:

```text
durable Connection Hub bundle storage
  versions/<version>.json + complete active.json
  authoritative history and recovery source
                         |
                         v
Redis serving projection
  delegated card:<access_id>             card + catalog_version
  delegated catalog:active               complete active.json, with TTL
  delegated catalog:version:<version>    one key per historical version, with TTL
```

There is no process catalog cache. `on_app_deploy` already owns the effective
`connections` object in memory, and re-reads it inside the shared critical
section before publishing, so a publisher that captured an earlier generation
registers what is current rather than its own snapshot under a later version
stamp. It writes the durable version and complete `active.json`, caches the
immutable version in Redis, and atomically caches the complete active document.
A matching durable version is not enough to declare deployment ready: the Redis
active document must also be present. If a cache
entry later expires, the relevant request uses the validated durable
read-through and stores it again with the configured TTL.

Many immutable `catalog:version:<version>` keys can coexist. Every write or
read-through restoration assigns the configured historical-catalog cache TTL;
a cache hit does not extend it. When an entry expires, the next list/open that
needs that exact version validates its durable version document and caches it
again. Catalog-cache lifetime is independent of card lifetime.

The active catalog uses the same fixed-residency rule: publication or validated
read-through assigns its descriptor-owned TTL, while ordinary request hits do
not extend it. Catalog TTL expiry is cache eviction, not a catalog change. A
new `on_app_deploy` publication atomically replaces `catalog:active` and leaves
all immutable historical keys untouched.

Connection Hub publishes through the fleet-coordinated `on_app_deploy`
readiness barrier. That barrier reconciles every deployment-scoped resource for
the current source/effective-props generation, including app-owned catalog and
index builders plus the platform-owned deployed UI inventory and artifacts. It
commits the app generation only after every required resource is ready. Each
resource family has its own signature, so repeated deployment reuses unchanged
artifacts while props can still attach, remove, or reconfigure UI components.
There is one deployed widget-delivery contract and no delivery-mode branch.

The lifecycle rule is independent of resource type: `on_app_deploy` ensures
that all resources of the app generation are ready. App hooks and platform
reconcilers are implementation participants beneath that single contract.

A props-update event may separately call `on_props_changed` on a cached
instance for process-local reconciliation; `on_bundle_load` remains
per-process initialization and does not publish catalog state. The same event
also invokes coordinated `on_app_deploy` regardless of singleton mode.

Card list/create/update and governed-operation paths never publish or modify
durable catalog history. They may restore an expired Redis cache entry from an already
committed document. They consume the registered active catalog represented by
cached `active.json`.
Effective props participate only in `on_app_deploy` alignment; they are not a
request-time authorization or drift input.

Every card and governed request reads the live card and TTL-cached
complete `active.json` from Redis. Governed execution validates
`content_hash == hash(connections)` and computes
`intersect(active.json.connections, card)`. It does not load another active
body, inspect effective props, or consult catalog history.

If cached `active.json` expired, the request may read complete committed
durable `active.json`, validate it, and atomically cache that complete document
with a TTL. This restores the Redis serving projection; it does not create a
version or change durable catalog history. The Redis update refuses to replace
a newer sortable catalog version with an older delayed restoration.

That no-downgrade comparison protects only the single Redis `catalog:active`
key. It does not compare a card's saved version with the active version.
Governed execution never loads the card's historical catalog; list/open uses
the separate `catalog:version:<card.catalog_version>` key only for drift.

List/open computes `diff(active.json, card)`. When it must distinguish a newly
added option from one that already existed but was left unselected, it reads
the historical Redis document named by `card.catalog_version`. If that cache
entry expired, it reads exactly the corresponding durable
`versions/<card.catalog_version>.json` and repopulates Redis. This historical
read is only for drift explanation; governed execution uses the exact
materialized authority already stored in the card.

The historical key is shared by every card on that version and its TTL is
fixed when the key is populated. A miss performs the durable read-through and
repopulates the key with a fresh configured TTL; requests after repopulation
reuse it until the next expiry. Multiple historical versions remain cached
under distinct keys.

Failure to obtain or validate a required active or historical document returns
structured unavailability. No coroutine or lock exists per catalog entry; the
async request simply awaits Redis and, on TTL miss, durable storage.

To say that an operation is **new since the last grant**, list compares the
card's referenced immutable version with complete `active.json`. Comparing only
the current catalog with selected operations can find removed selected values,
but cannot distinguish a newly added operation from one that was already
available and deliberately left unchecked.

### Per-resource accepted state

Every card resource has its own descriptor authority. A static catalog row
follows the deployment catalog version; a user-owned connector follows its own
descriptor revision and does not appear in catalog history at all. Comparing a
card only against the catalog version therefore had two failures: an unrelated
catalog change made an unchanged connector's grants read as newly available
(the baseline document never contained the overlay row), and a changed
connector tool under an unchanged catalog version read as current.

The card now records `resource_acceptance` at every save:

```text
resource_acceptance[<resource>]
  kind        catalog | remote_mcp        which authority describes it
  provider    ""      | remote_mcp
  revision    catalog version | connector descriptor revision
  digest      row digest (grants, tools, named services, identity)
              | connector descriptor digest
  grants      the claim ceiling seen
  operations  <operation> -> descriptor digest, for every offered operation
```

`catalog.descriptors` digests a static row from its published projection,
deliberately excluding the catalog version, and reads a connector row's own
evidence from the overlay (`remote_mcp.catalog.RemoteMCPResourceRow`). Drift
(`catalog.drift.card_drift`) then judges each resource against its acceptance
and returns a `resources` block with one entry per resource
(`current`, `changed`, `removed`, or `unknown` for a card written before this
evidence existed), and a `changed.outer_operations` block for selected
operations whose descriptor changed. Such an operation carries the effect
`suspended_until_accepted`: it stays granted on the card but is not to be run
until the grantor accepts exactly that change, through `accepted_operations`
on save. A newly advertised operation is reported under `added` and stays
ungranted. An unrelated catalog change leaves an unchanged resource `current`.

### Drift projection and Save concurrency

`delegated_access_list` computes drift on the server and returns one status per
card:

| Status | Meaning |
| --- | --- |
| `current` | The card references the active catalog version. |
| `changed` | A relevant resource, claim, outer operation, or named operation changed, including a selected operation whose descriptor changed on its own authority. |
| `no_relevant_change` | The global catalog advanced without changing anything represented by this card. |
| `baseline_missing` | The card predates `catalog_version`, or its referenced durable version document is confirmed absent, so exact additions since the previous Save cannot be identified. |
| `unavailable` | Complete cached `active.json`, or a historical version required for drift, cannot be obtained from Redis or restored from committed durable state; or a catalog document fails content-hash validation. Editing authority is disabled; create/update and governed calls return `503 temporarily_unavailable` when the unavailable document is required for their decision. |

The drift object contains ready-to-render `removed` and `added` entries.
Removed selected entries stay visible as disabled/stale rows and are already
ineffective. Added entries are current options but remain unchecked. Current
provider/account requirements are rendered from live discovery instead of
catalog history.

An edit submits `expected_card_revision` and
`expected_catalog_version`. Save returns `409` with a refreshed projection
if either the card or catalog changed after the editor loaded. Otherwise the
server prunes stale selections, validates every survivor against the active
catalog, rebuilds `operations` and `named_services`, increments
`card_revision`, and stamps the active `catalog_version` atomically.

Create and Save write the durable revision before the projection, the grantor
index, and the credential handles. A failure in any of those three returns
`503 delegated_card_serving_state_unavailable` and names the `access_id`: the
committed revision is the authority and a read that misses its projection
reloads it, but the caller's own step did not complete, so a client holding
credential material it never received can name the card that exists without it.

Runtime applies the same ceiling before Save:

```text
effective resource claims       = stored claims intersect active claims
effective outer operations      = stored operations intersect active operations
effective named operations      = stored selections intersect active namespace operations
effective account-backed access = stored account_scope checked against current requirements
```

What each row bounds, when its block is empty or absent:

| Dimension | Rule |
| --- | --- |
| Resource claims | Bounded by the row's `grants`, which the parser derives from its tools and namespace operations when they are not written. A row that publishes no claim publishes none. |
| Outer operations | The all-resource row `*` carries no ceiling: its operations come from endpoint policy, not the catalog, and it answers only a card whose own selector is `*`. Every other row is bounded by the tools it publishes, and a block that was emptied or deleted publishes none. |
| Named-service operations | Bounded by the published namespaces always. An absent or empty block publishes nothing; unlike outer tools there is no second source for an inner operation. |

Emptying a block and deleting it are one withdrawal written two ways, and the
guard answers them identically. Drift reads each dimension the same way the
guard does, so a capability the deployment withdrew is both refused at the call
and shown on the card as repairable.

A governed request returns different structured outcomes for policy change and
catalog failure:

| Condition | Response |
| --- | --- |
| Current `active.json` cannot be obtained or validated. | HTTP `503`, `temporarily_unavailable`, retryable after shared-state recovery. |
| The requested capability is present in the card's exact stored authority but absent from current `active.json.connections`. | HTTP `403`, `delegated_capability_no_longer_available`, non-retryable until discovery, card, or service configuration changes. |
| The capability is current but absent from the card. | HTTP `403` using the existing missing-grant/consent denial. |

The removed-capability response names the failed dimension, resource,
namespace/operation where applicable, `card_catalog_version`,
`active_catalog_version`, and a recovery action to refresh discovery or review
delegated access. It does not emit a consent action because additional user
consent cannot restore a capability removed by current service configuration.

Its structured `requested_capability` object contains the complete path:

| `kind` | Required fields |
| --- | --- |
| `resource` | Configured `resource`; concrete `request_resource` when available. |
| `resource_claim` | `resource`, `claim`; `request_resource` when available. |
| `outer_operation` | `resource`, `surface`, `outer_operation`; `request_resource` when available. |
| `named_service_namespace` | `resource`, `surface`, `namespace`; outer/request fields when applicable. |
| `named_service_operation` | `resource`, `surface`, `namespace`, `operation`; outer/request fields when applicable. |

`resource` is the matched card/catalog selector. `request_resource` is the
concrete URL or resource identifier supplied by the transport. Both are
returned when wildcard matching was involved.

The response also carries `reason`, opaque `access_id`, `card_revision`, both
catalog version ids, and structured recovery with `retry_same_request: false`.
An operation name alone is insufficient because the same operation can exist
under multiple resources or namespaces.

Those fields come from three request-time inputs:

| Field | Source |
| --- | --- |
| Card ids/revision/saved catalog version | The resolved live card. |
| Active catalog version and current resource policy | Complete validated `active.json`. |
| Configured resource selector | Canonical matching across card and active catalog. |
| Concrete request resource and outer operation | REST/MCP request target and operation dispatch. |
| Named-service namespace and inner operation | Parsed `NamedServiceRequest`, before provider invocation. |
| Claim | The exact current resource/operation policy check that failed. |

The managed REST/MCP guard checks resource, claims, and outer operation. Every
common named-service dispatcher then requires explicit admission after request
decoding and before provider selection:

| Entrance | Admission construction and inner check |
| --- | --- |
| Managed MCP door | The guard retains a sanitized immutable snapshot of the exact live card and `ActiveCatalogCapabilities` already accepted for this singular request. The bridge reuses that snapshot at the common dispatcher for the decoded namespace and effective operation. |
| Native hosted-agent tool | Each `_call` constructs delegated admission from trusted source bundle, agent, client, and user identity. Direct execution asks Connection Hub once for that invocation; a relayed execution carries the selector and resolves it once at the target. |
| Data Bus relay target | The handler validates the typed selector against the restored actor, resolves the exact current card/catalog decision through Connection Hub, binds its account scope, and invokes the provider. |
| Application-owned callers | The trusted call site constructs `NamedServiceAdmission.application(...)` and dispatches under application authority. |

The inner check uses the decoded `NamedServiceRequest`; tool names remain outer
surface routing. Admission is platform-owned dispatch state and stays separate
from request context and provider payloads.

For a bearer-authenticated delegated client, the request/session projection
carries `identity_authority.delegated_card_binding` with the exact `access_id`,
delegated client, grantor, delegate, and expiry metadata established by the
managed guard. A relay selector must match that binding. Connection Hub then
loads that exact card rather than selecting another card owned by the same
user. The binding records authenticated identity; current grants are resolved
again for each invocation.

No historical catalog body is required on this request path. Exact membership
in the card proves the capability belonged to its stored selection; absence
from complete current `active.json.connections` proves it is no longer exposed.
The version ids provide provenance. Historical catalog content is loaded only
for list/open drift details.

The clamp applies to pointer-backed card records and to older managed grant
bindings still accepted by compatibility paths. The generic guard consumes a
catalog-resolver interface and does not know which storage technology or bundle
produced the current projection.

There is no delegated-card selector meaning "all current and future operations
in this namespace." New operations therefore remain ungranted. Platform
administrator authority is a separate concept and must not be inferred as a
future-operation wildcard on a granular card.

Immutable card revisions are required for durable authority and provenance.
They do not expose rollback: restoring old authority would be a new explicit
grant operation, not a pointer move to a historical revision.

## Read Model For Gateway And Projection

`connection_hub.delegated_credentials.cards.read_model` is the one portable
view of a card that its consumers read. Gateway lists and routes tools from
it; Projection intersects it with descriptor and conversation ceilings. Neither
parses persistence records nor reproduces identity, acceptance, or migration
rules.

```text
DelegatedCardView
  caller_kind                   resident | oauth | manual | credentialless
  profile                       ResidentCallerProfile for a resident card
  access_id, card_revision, catalog_version, state, source, label
  created_at, expires_at, identity_scope
  resources[]
    resource, kind, provider, label, identity_scope, state
    grants
    operations[]                name, state (current|changed|removed|unknown),
                                accepted_digest, current_digest, policy (public)
    accepted_revision, current_revision, accepted_digest, current_digest
    named_service_operations    namespace -> operations
  account_scope
  control_card                   optional link to one credentialless Card
  project_control                compatibility name for its resolved live view
  provenance
```

`AutomationAccessService.describe_card(user, access_id=...)` returns the view of
a card its grantor owns; `resident_profile_card(grantor_subject, client_id)`
returns the view of one resident profile's card (the stable card first, else a
single not-yet-folded legacy record, else nothing).
`compatible_resource_offers` lists which owner-visible catalog resources may
join a Card and why the others may not. Every field is non-secret.

## Revocation And Expiry

- An expired card ceases to resolve: Redis drops its live projection and the
  resolver refuses the durable revision, so no guard, gateway or picker sees
  it. The durable revision itself remains, with every grant, selection,
  account binding and policy the card held.
- The owner's list (`delegated_access_list`) keeps showing an expired card,
  flagged `expired: true`, until it is revoked. Why: the grants are the
  grantor's work and the token is only the key, so expiry must not cost the
  work. The list reads durable membership through `list_current`, which
  admits expired cards and excludes revoked ones; guards and pickers keep
  reading `list_active`.
- **Renewal** (`delegated_access_renew`, `AutomationAccessService.renew_access`)
  brings a card's credential back, two ways, chosen by `mode`:
  - `prolong` keeps the credential a connected app already holds and
    extends its life: the card's `expires_at`, the app's refresh token and
    its current access binding get `ttl_seconds` more (default: the card's
    previous lifetime). Nothing on the client changes, which is the point
    for a client whose token is buried in its own configuration. It works
    only while the refresh token still exists; an ended one answers
    `delegated_access_credential_expired` (reconnect from the client, onto
    the same card). A manual or agent bearer carries its own end date inside
    the token, so it is never prolonged (`delegated_access_prolong_unsupported`).
  - `reissue` issues a fresh bearer on an existing manual card, expired or
    live, keeping the card, its `access_id` and everything it holds. It
    reads the durable current revision through `load_current` (any state),
    refuses a revoked card (`delegated_access_revoked`) and another owner's
    card (`delegated_access_not_found`), mints the new bearer with the
    card's own authority, commits the next revision with the new
    `expires_at`, `last_issued_at` and `last_four`, retires the previous
    session, and returns the token once, as at creation. Only manual cards
    reissue here (`delegated_access_renew_unsupported` otherwise).
  Both count in `provenance` (`prolongations`, `renewals`). Editing a card
  is independent of its expiry: `delegated_access_update` works on an
  expired card, and the credential comes back by renewal.

## Implementation Map

| Concern | Implementation |
| --- | --- |
| Capability/resource vocabulary and parser aliases | `connection_hub.delegated_credentials.oauth.config` |
| Card operations and authority orchestration | `connection_hub.delegated_credentials.automation_access` |
| Authority resolution shared by every save | `automation_access.AutomationAccessService._resolve_card_authority` |
| Durable revisions and current pointer | `...delegated_credentials.cards.store.DelegatedCardStore` over Connection Hub bundle storage |
| Resident bearer metadata, host-secret custody, rotation, and cleanup | `...delegated_credentials.cards.handle_authority`, `.handle_metadata`, and `.resident_secrets` |
| Persistence port, TTL live projection and read-through | `...delegated_credentials.cards.persistence`, `.cache`, `.handles`, `.resolver` |
| Stored selection states and card model | `...delegated_credentials.cards.model` |
| Credentialless Card creation, historical exact-snapshot migration, and Card composition | `...delegated_credentials.controls.snapshot`, `.model`, and `.effective`; current serving uses the ordinary Card persistence/cache |
| Stable resident caller identity | `...delegated_credentials.cards.identity` |
| Per-resource accepted descriptor state | `...delegated_credentials.catalog.descriptors` |
| Portable card read model and compatible-resource offers | `...delegated_credentials.cards.read_model` |
| Resident-profile fold of legacy records | `automation_access.AutomationAccessService.migrate_resident_profile` |
| Renewal of a manual card, expired or live | `automation_access.AutomationAccessService.renew_access`; owner reads that see expired cards: `cards.persistence.DurableCardPersistence.load_current`, `.list_current` |
| Pre-encoding record interpretation | `...delegated_credentials.cards.migration` |
| Durable catalog history and publication | `...delegated_credentials.catalog.store` and `.publisher`, immutable version documents and complete `active.json` |
| Current catalog resolution and read-through recovery | `...delegated_credentials.catalog.resolver` |
| Current-capability denial shaping | `...delegated_credentials.catalog.authorization.authorize_current_capability` and `card_boundary_denial` |
| Save-time drift and reconciliation | `...delegated_credentials.catalog.drift`, `.reconcile` |
| OAuth consent view model and contract | `...delegated_credentials.oauth.consent`, `.http.routes` |
| Named-service strict narrowing and materialization | `...delegated_credentials.named_service_policy` |
| Descriptor namespace/operation projection | `...solutions.named_services_providers.boundary_policy.NamedServiceBoundaryCatalog` |
| Live provider requirement enrichment | `automation_access.AutomationAccessService._named_service_options` plus provider discovery `spec.metadata.connected_accounts` |
| Pointer-backed live card resolution | `...delegated_credentials.live_grant` |
| Once-or-always policy and invocation idempotency | `connection_hub.invocation_policy` |
| Owner-scoped external MCP resource overlay | `connection_hub.remote_mcp.catalog` |
| Managed request projection | `...delegated_credentials.oauth.surface_guard._live_grant_record` |
| Generic named-service admission contract | `...solutions.named_services_providers.admission` |
| Connection Hub admission resolvers and managed snapshot | `...solutions.connections.named_service_admission` |
| Relayed selector validation and target resolution | `...solutions.named_services_providers.relay` |
| Connection Hub operations and descriptor binding | [`products/connection-hub/apps/connection-hub@1-0/entrypoint.py`](../../../products/connection-hub/apps/connection-hub@1-0/entrypoint.py) |
| List/create/edit UI and account-requirement fusion | `connection-hub@1-0/ui/widgets/connections/src/features/delegatedAccess` |
| Connected-account credential resolution | delegated-to-KDCube broker and provider adapters |
