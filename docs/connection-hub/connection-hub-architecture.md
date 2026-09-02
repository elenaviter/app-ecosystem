---
id: connection-hub/connection-hub-architecture
title: "Connection Hub Architecture And Semantic Requirements"
summary: "Canonical architecture for Connection Hub: identity and delegation semantics, public and managed surfaces, user-owned external MCP proxying, storage authorities, host boundaries, and direct protected-service admission."
status: active
tags: ["connection-hub", "architecture", "identity", "delegated-access", "storage", "admission", "mcp", "proxy"]
keywords: ["connection edge", "connected account", "delegated card", "active capability catalog", "external MCP connector", "invocation policy", "protected service", "direct admission", "storage authority"]
updated_at: 2026-09-02
see_also:
  - ./package/delegated-authority-and-admission.md
  - ./package/delegated-cards.md
  - ./package/oauth-delegated-credential-protocol.md
  - ./frontend/application/storage/README.md
  - ../../products/connection-hub/apps/connection-hub@1-0/interface/README.md
---

# Connection Hub Architecture And Semantic Requirements

Connection Hub is the Connection Hub-backed application through which users and
operators manage identity connections, connected provider accounts, and
delegated access. It is currently hosted as the KDCube app
`connection-hub@1-0`. Connection Hub owns portable authority models and decisions;
KDCube supplies request authentication, app storage, user properties and
secrets, Redis, Postgres, routing, and UI hosting.

This page owns the whole-system architecture. The package and interface pages
own their individual APIs and data models.

## Semantic Model

Connection Hub maintains several related contracts. They are not one generic
"connection" record.

| Contract | Question answered | Authority |
| --- | --- | --- |
| Connection edge | Which platform user is linked to this verified external identity? | A verified `authority + subject -> platform user` edge. |
| Request authenticator | Did this request prove an identity under a configured authority? | Provider-specific verification selected from descriptor and Postgres metadata; secret material stays in app secrets. |
| Connected account, delegated TO KDCube | May trusted server-side code use this user's external provider account, and with which provider claims? | User-scoped account metadata plus a server-side credential. |
| Delegated card, delegated BY a user | Which caller may use which resource, operation, grants, and optional connected-account scope? | The card's current immutable revision. |
| Active capability catalog | What may this deployment delegate now? | The current operator-published catalog version. It is the ceiling over every card. |
| User-owned external MCP connector | Which remote MCP endpoint and accepted tools may Connection Hub present as a guarded resource for this user? | The connector's current immutable revision plus a separately stored upstream credential. |
| Invocation policy | May this already-granted operation be invoked repeatedly or one time? | A live policy record keyed to the exact card, resource, operation, and optional connected account. |
| Protected-service registration | Which authenticated backend may ask about which catalog resources? | Descriptor-owned workload registration with resource selectors only. |

The effective delegated authority for one operation is the intersection of
current facts:

```text
authenticated delegated bearer
  AND current, unrevoked card revision
  AND requested resource on that card
  AND requested operation on that card
  AND operation still present in the active catalog
  AND grants allowed by both card and catalog
  AND requested connected account allowed by the card, when present
  AND current connector state and accepted tool descriptor, when the
      user-owned external MCP proxy is used
  AND the operation's current once-or-always invocation policy
  AND authenticated protected service registered for that resource,
      when the direct admission surface is used
```

Absence at any required leg is a denial. Authentication proves an actor; it
does not authorize every operation.

## Required Security Properties

1. **Default closed.** Missing card state, missing catalog state, unavailable
   storage, a missing service secret, or an incomplete account selection does
   not become an allow.
2. **Live authority.** Managed guards and direct admission resolve the current
   card and active catalog for each consequential call. Editing, narrowing, or
   revoking a card affects the next decision.
3. **Two proofs for direct admission.** The delegated bearer proves the caller
   and user grant. A separate signed workload request proves the protected
   service and binds the bearer, resource, operation, and account request.
4. **Server-side credentials.** Provider access tokens, refresh tokens,
   app-passwords, OAuth client secrets, signing secrets, and service signing
   secrets are never returned in admission decisions.
5. **No client-authored authority.** A caller may request a resource,
   operation, and account. It may not supply a trusted user id, card revision,
   grants, roles, or catalog version.
6. **Bounded identity projection.** A directly integrated backend receives a
   stable service-scoped user subject and caller-profile id. It receives
   neither the internal platform user id nor the raw delegated-card id.
7. **One policy path.** KDCube-managed REST/MCP guards and direct external
   admission use the same live delegated-card and catalog evaluator. The
   public route does not maintain a second capability model.
8. **Domain authorization remains local.** An allow authorizes the named
   operation at the registered resource. The protected backend still checks
   domain objects and must perform the operation it asked about.
9. **Accepted external MCP descriptors.** Connection Hub compares the live
   descriptor of the exact tool with the user's accepted descriptor before a
   proxied call. A changed or removed tool fails closed; a newly advertised
   tool remains ungranted until the user accepts the descriptor and grants it.
10. **One-use authority is separate from card authority.** A card answers
    which operation may be called. Its invocation policy answers whether that
    operation may be called repeatedly or has one remaining use.

## Runtime Architecture

```text
 USERS AND CALLERS

 browser user          external identity       agent / app / automation
     |                         |                         |
     | platform session       | provider proof          | opaque kst1 bearer
     v                         v                         v
+---------------------------------------------------------------------------+
| CONNECTION HUB APP SURFACES                                               |
|                                                                           |
| Connections widget       request_authenticate       delegated OAuth       |
| authenticated ops        public proof callbacks     direct admission      |
| connections named svc    external MCP connectors   remote MCP proxy      |
|                                                     discovery metadata    |
+-------------------------------+-------------------------------------------+
                                |
                                | KDCube host adapters
                                v
+---------------------------------------------------------------------------+
| CONNECTION HUB AUTHORITY LIBRARY                                          |
|                                                                           |
| connection edges     connected-account contracts     request authenticators|
| delegated cards      active capability catalog       admission decisions  |
| invocation policy    external MCP contracts          bounded projections  |
| OAuth contracts      structured denials              descriptor drift     |
+-------------------------------+-------------------------------------------+
                                |
                                | host storage and runtime ports
                                v
+---------------------------------------------------------------------------+
| KDCUBE HOST                                                               |
|                                                                           |
| app storage       user props/secrets       Postgres       Redis            |
| request/session authority               secret resolver  routing/UI       |
+---------------------------------------------------------------------------+

 DIRECT EXTERNAL PROTECTED SERVICE

 external caller -- bearer --> protected backend
                                  |
                                  | bearer + signed service proof
                                  | resource + operation + optional account
                                  v
                           Connection Hub admission
                                  |
                    current card + current catalog decision
                                  |
                       allow with bounded principal
                                  v
                     backend enforces domain operation

 USER-OWNED EXTERNAL MCP SERVICE

 delegated caller -- bearer --> Connection Hub remote MCP proxy
                                  |
                     current card + invocation policy
                     accepted descriptor + connector state
                                  |
                     upstream credential injected here
                                  v
                            remote MCP server
```

The direct backend is a policy-enforcement point. If that backend cannot be
trusted to perform the operation it asked about, route the operation through a
KDCube-managed REST or MCP door instead, where authorization and dispatch are
co-located.

## Surface Inventory

| Surface | Caller and proof | State consulted | Result |
| --- | --- | --- | --- |
| `connections_settings` widget | Browser with platform session | All user-facing connection/card metadata; never provider secrets | Connect, inspect, edit, narrow, or revoke. |
| Authenticated `operations/*` aliases | Platform-authenticated browser or trusted app caller, plus visibility guard | Connection edges, authenticator metadata, connected accounts, cards, catalog | User and operator management operations. |
| `request_authenticate` | Gateway or app with provider proof and optional authenticator selector | Authenticator metadata and secret ref, verifier, connection edge | Authenticated actor and linked platform authority. |
| Provider proof and OAuth callbacks | External provider redirect or signed provider request | Short-lived flow state, provider configuration, user account store | Complete identity-link or connected-account flow. |
| Delegated OAuth authorization server at `public/oauth/*` | OAuth client plus browser consent | Client registration, codes, refresh/access bindings, cards, catalog | Opaque delegated bearer and refresh lifecycle. |
| `connections` named-service provider | Trusted KDCube app/tool acting for current user | User account metadata and server-side credential broker | Provider operation result or actionable consent requirement. |
| Managed REST/MCP guard | Request carrying delegated bearer to a KDCube surface | Access binding, current card, active catalog, surface operation | Continue dispatch or structured denial. |
| External MCP connector operations | Browser with platform session | Owner-scoped connector revisions, accepted descriptors, secret references | Create, inspect, refresh, accept drift, disable, rotate credentials, or delete. |
| `remote_mcp_proxy` | MCP client carrying delegated bearer | Exact card, connector, accepted and live tool descriptors, invocation policy, upstream secret | List only granted tools and invoke the selected upstream tool. |
| `delegated_admission` | Registered external backend carrying delegated bearer and signed service proof | Same live guard state plus service registration and replay store | Bounded allow/deny decision; no provider credential. |
| OAuth/protected-resource metadata | Public discovery client | Descriptor-derived OAuth and resource metadata | OAuth endpoints, supported capabilities, and direct-admission endpoint when enabled. |

The app's human interface contract is
[`products/connection-hub/apps/connection-hub@1-0/interface/README.md`](../../products/connection-hub/apps/connection-hub@1-0/interface/README.md).

## User-Owned External MCP Proxy

This mode covers a remote MCP service that has no Connection Hub admission
integration. The user registers its streamable-HTTP endpoint and, when needed,
an upstream bearer, header credential, or OAuth connection. Connection Hub
discovers the tools, stores an accepted descriptor snapshot, and adds one
owner-scoped resource to the user's card editor:

```text
urn:connection-hub:remote-mcp:<connector-id>
  grant: external_mcp:use
  operations: exact accepted upstream tool names
```

The upstream credential is stored in the user's server-side secret store. The
durable connector revision contains only an opaque credential reference and
non-secret descriptor metadata. A delegated caller receives a Connection Hub
bearer for its own card; it never receives the upstream credential.

For OAuth connectors, Connection Hub follows the MCP protected-resource and
authorization-server discovery chain. It uses its public Client ID Metadata
Document when the server advertises URL-based client ids, otherwise it uses
dynamic client registration. The browser authorization uses PKCE and a random
single-use state whose transaction body remains in the owner's secret store.
The callback exchanges the code server-side, then connector discovery begins
with the resulting access token. Access-token refresh is serialized by a
connector-scoped cross-worker lock and refresh-token rotation is written back
to the same user secret. Reauthorization keeps the connector resource stable
and revokes the replaced upstream grant when the provider exposes revocation.
Connector deletion revokes the current access and refresh tokens before the
local secret is removed. Provider revocation failure never preserves local
connector authority.

This upstream OAuth flow authenticates Connection Hub to the remote MCP
server. The separate delegated OAuth server at `public/oauth/*` authenticates
an external caller to Connection Hub and creates that caller's delegated card.

For every list or call, the proxy resolves the exact current card and connector
for their common owner. A tool is visible and callable only when all of these
facts hold:

1. the connector is active;
2. the card contains its exact connector resource and `external_mcp:use`;
3. the card selects the exact upstream tool under that resource;
4. the tool is still advertised with the accepted descriptor digest;
5. the current invocation policy allows this invocation.

Refresh stores added, changed, and removed tools as pending descriptor drift.
An unchanged selected tool remains usable when another tool drifted. A changed
or removed selected tool is denied. Accepting the pending descriptor advances
the connector descriptor revision; newly accepted tools then appear in the
card editor unchecked. Descriptor comparison detects changes in advertised
name, description, and schemas. It cannot detect an implementation change that
keeps the same advertised descriptor.

The proxy validates the endpoint against deployment-owned outbound policy at
creation, refresh, observation, and invocation. Public HTTPS is the default.
HTTP, private networks, and explicit host exceptions are deployment choices,
not user-provided overrides. The host resolves and validates every DNS answer
again when opening the TCP socket, then connects to that approved address while
retaining the original hostname for TLS verification. Redirects and
environment-provided HTTP proxies remain disabled for this transport.

Because Connection Hub performs the upstream call in this mode, it can store a
terminal result under the invocation id. A retry with the same id and request
digest returns that result without calling the remote MCP tool again. Reusing
the id with different arguments is denied.

## Direct Protected-Service Admission

Direct admission supports a backend that is not itself behind a KDCube app
door:

```http
POST /api/integrations/bundles/{tenant}/{project}/connection-hub@1-0/public/delegated_admission
Authorization: Bearer <opaque-kst1-token>
X-Connection-Hub-Service-Id: crm-api
X-Connection-Hub-Timestamp: 1788048000
X-Connection-Hub-Nonce: 4b38d173e0864ee891f23f17
X-Connection-Hub-Signature: <base64url-hmac-sha256>
Content-Type: application/json

{
  "resource": "https://api.example.test/customers",
  "operation": "customers.search",
  "invocation_id": "customer-search-0185",
  "request_digest": "<sha256-of-the-provider-domain-request>",
  "account": {
    "provider_id": "salesforce",
    "account_id": "account-17",
    "claims": ["contacts:read"]
  }
}
```

`account` is optional. When supplied, `provider_id` and `account_id` are both
required. Requested claims must be a subset of the live card's account scope.
`invocation_id` and `request_digest` are also optional as a pair. They are
required when the operation is governed by a one-use policy and recommended
for retryable writes.

### Service request signature

The first profile uses HMAC-SHA256 with a distinct secret per protected
service. The secret contains at least 32 bytes and is resolved server-side from
`secret_ref`.

```text
body = canonical JSON of resource, operation, optional account,
       and optional invocation id + request digest

message =
  "connection-hub-admission-v1\n" +
  service_id + "\n" +
  decimal_unix_timestamp + "\n" +
  nonce + "\n" +
  sha256(delegated_bearer) + "\n" +
  sha256(body)

signature = base64url_without_padding(HMAC-SHA256(service_secret, message))
```

The default timestamp window is 300 seconds. A `(service_id, nonce)` may be
used once and is retained for 600 seconds. Replay state is stored in Redis and
fails closed when Redis is unavailable. TLS remains required; the signature is
request authentication and binding, not transport encryption.

Service authentication and resource registration are checked before the
delegated bearer is inspected. This prevents an unregistered workload from
using the endpoint as a bearer-probing oracle.

### Allow response

```json
{
  "ok": true,
  "allowed": true,
  "schema": "connection_hub.delegated_admission.v1",
  "decision_id": "opaque-correlation-id",
  "service_id": "crm-api",
  "principal": {
    "sub": "prk_sub_service_scoped_value",
    "client_id": "prk_client_service_scoped_value"
  },
  "authority": {
    "resource": "https://api.example.test/customers",
    "operation": "customers.search",
    "grants": ["crm:read"],
    "account_scope": {
      "provider_id": "salesforce",
      "account_id": "account-17",
      "claims": ["contacts:read"]
    }
  },
  "provenance": {
    "card_revision": 7,
    "card_catalog_version": "delegated_catalog_...",
    "active_catalog_version": "delegated_catalog_..."
  },
  "expires_at": 1788049000
}
```

The response does not expose the platform user id, raw card access id, raw
caller client id, identity-family scope, bearer, or provider credential.
`principal.sub` is a stable pairwise id for this service and user.
`principal.client_id` is a separate stable pairwise id for this service, user,
and delegated caller profile. The same user can therefore authorize several
caller profiles that the service can distinguish without learning their
Connection Hub ids. Neither pairwise id correlates the user or profile across
registered services.

Denials use `connection_hub.delegated_admission.v1`, a stable error code, a
`decision_id`, and a retryability flag. They do not disclose another card's
contents or another service's registration.

### Descriptor-owned registration

```yaml
connections:
  delegated_credentials:
    admission:
      enabled: true
      identity_projection_secret_ref: connections.delegated_credentials.admission.identity_projection_secret
      max_clock_skew_seconds: 300
      nonce_ttl_seconds: 600
      services:
        crm-api:
          enabled: true
          label: Customer API
          secret_ref: connections.delegated_credentials.admission.services.crm-api.signing_secret
          resources:
            - https://api.example.test/customers*
```

The service row binds a workload to resource selectors. It does not declare
operations, grants, tools, or connected-account requirements. Those remain in
the active delegated capability catalog.

The referenced values belong in app secrets:

```yaml
connections:
  delegated_credentials:
    admission:
      identity_projection_secret: "<random-32-byte-or-longer-secret>"
      services:
        crm-api:
          signing_secret: "<random-32-byte-or-longer-secret>"
```

The projection secret is separate from service signing secrets so rotating
one workload credential does not change that service's pairwise user or
caller-profile identifiers.

### Protected-service integration

A backend integrating this surface performs these steps:

1. The operator publishes the backend resource, operations, and grants in the
   active delegated capability catalog.
2. The operator registers the backend's service id, signing-secret reference,
   and resource selectors in Connection Hub. The signing secret stays in the
   backend's secret store and the Connection Hub app secret provider.
3. The user authorizes the external caller through the existing delegated
   OAuth and card flow. The caller presents the resulting opaque bearer to the
   backend on each protected operation.
4. The backend signs a fresh admission request over that bearer and the exact
   semantic operation, using a new nonce for every attempt.
5. On allow, the backend executes only the requested operation for the returned
   pairwise user and caller-profile ids. It does not treat the decision as a
   general session or cache it for a later operation.
6. On denial, the backend performs no protected effect. It may retry only a
   response explicitly marked `retryable`, with a fresh nonce.

Connection Hub can reserve a one-use decision and replay the same allow
decision for the same invocation id and digest. It does not execute the
backend's domain effect. A backend that changes state must use that invocation
id in its own idempotency ledger before applying the effect. This is what
prevents an uncertain client retry from applying the same provider-side change
twice.

The `connection-hub` package supplies the canonical request and signing functions:

```python
import time
import uuid

from connection_hub.delegated_credentials.admission import (
    AdmissionRequest,
    SERVICE_ID_HEADER,
    SERVICE_NONCE_HEADER,
    SERVICE_SIGNATURE_HEADER,
    SERVICE_TIMESTAMP_HEADER,
    sign_admission_request,
)
from connection_hub.invocation_policy import canonical_request_digest

service_id = "crm-api"
timestamp = str(int(time.time()))
nonce = uuid.uuid4().hex
decision = AdmissionRequest.from_mapping(
    {
        "resource": "https://api.example.test/customers",
        "operation": "customers.search",
        "invocation_id": "customer-search-0185",
        "request_digest": canonical_request_digest({"query": "north"}),
    }
)
signature = sign_admission_request(
    secret=service_signing_secret,
    service_id=service_id,
    timestamp=timestamp,
    nonce=nonce,
    delegated_token=opaque_bearer,
    request=decision,
)

headers = {
    "Authorization": f"Bearer {opaque_bearer}",
    SERVICE_ID_HEADER: service_id,
    SERVICE_TIMESTAMP_HEADER: timestamp,
    SERVICE_NONCE_HEADER: nonce,
    SERVICE_SIGNATURE_HEADER: signature,
}
body = decision.signing_dict()
# POST body and headers to the advertised delegated-admission endpoint.
```

The backend may use any HTTP client. The important interoperability contract is
that it sends `decision.signing_dict()` unchanged as the JSON body; changing
the resource, operation, account scope, bearer, timestamp, or nonce invalidates
the signature.

## Storage Authority Map

Connection Hub uses several storage classes because their lifecycles and
security properties differ.

| Store | Authoritative records | Rebuildable or short-lived records | Secret values |
| --- | --- | --- | --- |
| Effective app descriptor | Provider/connector definitions, claim vocabulary, OAuth resource catalog source, visibility, descriptor authenticators, direct-admission service registry | None | No; only `*_ref` pointers. |
| App secret provider | Provider OAuth client secrets, OAuth state secret, authenticator verifier secrets, service HMAC secrets, pairwise projection secret | Secret cache may be rebuilt | Yes, server-side only. |
| Shared app storage | Connection edges and challenges in the current implementation; immutable delegated-card revisions and current pointers; immutable catalog versions and active pointer | None | No credential values. |
| User properties and user secrets | Connected-account metadata in user properties; provider access/refresh tokens and app-password material in user secrets | Host secret caches | Yes, in user secrets only. |
| Postgres | Request-authenticator metadata rows and `secret_ref` values | Redis selector cache | No secret values. |
| Redis | OAuth authorization codes, dynamic client registrations, refresh-token records, opaque access-token grant bindings, live credential handles, consent CSRF state, direct-admission nonces | Card/catalog serving projections, authenticator selector cache, discovery, events, coordination | Contains bounded live credential/protocol records; never treat the whole database as a disposable cache. |
| External provider | Provider account, consent, and upstream token validity | Provider-specific session state | Provider is the upstream authority. |

The detailed key and table map is in
[`frontend/application/storage/README.md`](frontend/application/storage/README.md).

### Durable authority and live handles

Delegated cards and the capability catalog are durable, versioned authority:

```text
shared app storage
  delegated-cards/v1/grantors/<subject-hash>/cards/<access-id>/
    revisions/<immutable-revision>.json
    current.json

  delegated-catalog/v1/
    versions/<immutable-version>.json
    active.json
```

Redis holds serving projections of those documents and reads through to
durable storage when a projection is missing. Credential handles and OAuth
protocol records are different: they are live, TTL-bounded state and cannot be
reconstructed merely from a durable card. Losing them invalidates the affected
live credential or flow; it must never broaden authority.

The current connection-edge implementation stores JSON under the app storage
root. Its semantic contract is stable; its physical store can later be
replaced without changing callers.

## Source And Host Ownership

```text
repo:app-ecosystem
  products/connection-hub/packages/connection-hub/
    portable identity, card, catalog, OAuth, admission, and denial contracts

  products/connection-hub/apps/connection-hub@1-0/
    canonical Connection Hub app, routes, widget, descriptors, OpenAPI

repo:kdcube
  kdcube_ai_app.apps.chat.sdk.integrations.connection_hub/
    KDCube request/session, storage, Redis, secret, REST, MCP, and user-store adapters
```

The app depends on KDCube today because KDCube is its first host. A future
standalone service must supply equivalent ports; it must not fork Connection Hub's
authority semantics or create a second card/catalog store.

## Deliberate Distinctions

```text
authenticated actor          != authorization for every operation
connection edge              != connected provider account
connected-account claim      != delegated caller grant
delegated card               != active capability catalog
service registration         != capability declaration
managed KDCube guard         != direct external admission
admission allow              != release of provider credentials
Redis serving projection     != durable card/catalog authority
runtime decision evidence    != immutable compliance audit
```

These distinctions are requirements, not documentation vocabulary. An
implementation that collapses them changes the trust model.
