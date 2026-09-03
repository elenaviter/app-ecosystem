---
id: connection-hub/package/delegated-authority-and-admission
title: "Delegated Authority And Admission"
summary: "Delegated-card and invocation-policy decisions across managed REST/MCP surfaces, proxied external MCP services, direct protected-service admission, connected-account claims, native named-service tools, and relayed provider invocation."
status: current
tags: ["arch", "security", "admission", "connection-hub", "delegated-access", "mcp", "rest", "named-services", "data-bus"]
keywords: ["delegated authority", "managed surface guard", "delegated access card", "access_id", "active catalog", "resource grants", "resource operations", "MCP tool grants", "connected account claims", "NamedServiceAdmission", "Data Bus relay"]
updated_at: 2026-09-03
see_also:
  - ../connection-hub-architecture.md
  - ./delegated-cards.md
  - ./oauth-delegated-credential-protocol.md
  - https://github.com/kdcube/kdcube/blob/main/app/ai-app/docs/arch/security-and-trust-model-README.md
  - https://github.com/kdcube/kdcube/blob/main/app/ai-app/docs/sdk/solutions/connections/authenticated-mcp/authenticated-mcp-README.md
  - https://github.com/kdcube/kdcube/blob/main/app/ai-app/docs/sdk/solutions/mcp/platform-mcp-over-connection-hub-README.md
---
# Delegated Authority And Admission

This page owns delegated-card decision mechanics. The
[Connection Hub architecture](../connection-hub-architecture.md) owns the
whole-system semantic model, surface inventory, and storage boundaries. This
page connects durable Connection Hub state, request and session identity,
managed REST/MCP guards, direct external protected-service admission, plain
tools with connected-account claims, and named-service calls that may execute
directly or through the Data Bus.

The delegated card and catalog system is reusable beyond named services. A
managed surface author registers protected resources, outer operations/tools,
and grants. The same current card and active catalog then govern a conversation
MCP tool, a productivity MCP tool, a managed REST operation, or the outer door
of a named-service MCP surface. `NamedServiceAdmission` is the additional inner
contract used only when execution enters the common named-service dispatcher.

The focused documents remain the implementation references:

- [Delegated Access Cards](./delegated-cards.md)
  owns card/catalog storage, rendering, drift, mutation, and recovery.
- [Authenticated MCP](https://github.com/kdcube/kdcube/blob/main/app/ai-app/docs/sdk/solutions/connections/authenticated-mcp/authenticated-mcp-README.md)
  owns the full managed MCP configuration and connected-account consent chain.
- [Platform MCP Over Connection Hub](https://github.com/kdcube/kdcube/blob/main/app/ai-app/docs/sdk/solutions/mcp/platform-mcp-over-connection-hub-README.md)
  owns the reusable MCP-door pattern and its caller families.
- [Named Services From An Isolated Runtime](https://github.com/kdcube/kdcube/blob/main/app/ai-app/docs/sdk/solutions/kdcube-services/named-services-from-isolated-runtime-README.md)
  owns the Data Bus request/reply protocol, worker behavior, retries, and replay.
- [Named-Service Integration](https://github.com/kdcube/kdcube/blob/main/app/ai-app/docs/sdk/namespace-services/integration-README.md)
  owns provider discovery, request/response shapes, and provider implementation.
- [Cross-Runtime Context](https://github.com/kdcube/kdcube/blob/main/app/ai-app/docs/runtime/cross-runtime-context-README.md) owns the
  portable actor, routing, policy, and runtime-bootstrap context.

## The Distinct Facts

The architecture keeps these facts separate:

| Fact | Answers | Owner |
| --- | --- | --- |
| Actor identity | Who caused this work? | Ingress and carried request context / `AuthContext`. |
| Authority selector | Which delegated record applies? | Exact bearer card binding or trusted hosted-agent identity. |
| Delegated card | What did this user grant this caller? | Connection Hub delegated-card store. |
| Active catalog | What does this deployment currently expose? | Connection Hub delegated catalog published from effective descriptor connections. |
| External MCP connector | Which user-owned endpoint and accepted tool descriptors may be projected into this user's card catalog? | Owner-scoped connector revisions and server-side credential reference. |
| Managed surface decision | May this resource and outer REST/MCP operation run now? | Managed surface guard for this request/tool invocation. |
| Invocation policy | Is this granted operation reusable or limited to one invocation? | Separate live policy and idempotency registry keyed to exact card authority. |
| Protected-service identity | May this external backend ask about this catalog resource? | Descriptor-owned service registration plus signed workload proof. |
| Direct admission decision | May this bearer perform this concrete operation at that registered external backend? | Connection Hub direct-admission surface using the same current card/catalog evaluator. |
| Connected-account decision | Which account and provider claims may this tool use? | Account broker using declared tool requirements and the card's `account_scope`. |
| Named-service admission | May this decoded namespace and inner operation run now? | Common named-service dispatcher, when that subsystem is used. |
| Provider request context | What diagnostics and domain context accompany a named-service request? | `NamedServiceRequest.context`, visible to provider code. |

The managed surface decision is request-local guard state. For a named-service
call, `NamedServiceAdmission` is separate platform-owned dispatch state and a
sibling of the provider request. `NamedServiceRequest.context` remains
provider-visible domain and diagnostic context.

## State That Feeds A Decision

Two durable histories produce the current delegated decision. Redis is their
serving projection:

```text
OPERATOR DEPLOYMENT                                USER AUTHORITY
effective descriptor connections                  create / edit / revoke card
              |                                                |
              v                                                v
immutable catalog version + active.json           immutable card revision + current.json
              |                                                |
              +--------- durable Connection Hub storage -------+
                                       |
                              validated read-through
                                       v
                              Redis serving projections
                              active catalog + live card
```

The durable documents contain non-secret authority and provenance. Credential
handles, provider tokens, refresh tokens, and reusable session secrets remain
in their bounded credential/session stores. A Redis miss reads the committed
durable current document, validates it, and restores the serving projection.

The same card/catalog state feeds three enforcement dimensions:

```text
managed resource/tool authority
    = current card resource grants + resource-qualified outer operations
      INTERSECT complete current active catalog

connected-account authority
    = current card account_scope
      INTERSECT current tool/provider account requirements

named-service inner authority, when used
    = current card named-service selection
      INTERSECT current active namespace/operation catalog
```

The card records the user's explicit selection. The active catalog is the
deployment ceiling. A plain account-backed tool declares its current provider
requirements and resolves an account whose approved claims satisfy them. A
named-service call adds its inner namespace/operation boundary.

Outer operation names are qualified by protected resource in card authority.
An operation called `search` on resource A does not authorize an operation
with the same name on resource B. The flat `operations` list present in public
and token compatibility shapes is derived from that resource map and is not
used as an independent grant.

## Complete Managed-Surface Flow

The managed guard is the reusable outer admission layer. The protected surface
decides what happens after that layer:

```text
external MCP/REST client or resident agent connection
                         |
                         v
                 delegated bearer proof
                         |
                         v
authenticate credential/session
bind actor + exact delegated_card_binding.access_id
                         |
                         v
Connection Hub managed surface guard
  resolve exact current card
  load complete current active catalog
  match protected resource
  check identity, expiry, outer operation/tool, and required grants
                         |
             denied with a structured reason, or
                         |
                         v
request-local delegated identity and current authority
                         |
                         v
                    surface handler
                         |
       +-----------------+-----------------+-----------------+
       |                 |                 |                 |
       v                 v                 v                 v
plain domain tool  plain account tool named-service door custom app door
Conversation MCP  Productivity MCP   generic MCP tools  REST or MCP
conversations:read tool claim policy named_services:use app-owned grants
       |            + account broker       |                 |
       |                 |                 v                 +-- app storage/domain logic
       |                 v         decoded namespace/op      |
       |          card account_scope       |                 `-- optional account claims
       |          INTERSECT current        v
       |          provider requirements NamedServiceAdmission
       |                 |          + inner card/catalog check
       |                 v                 |
       |          connected provider       v
       |          operation           provider discovery
       |                                   |
       +-------------------+---------------+-----------------+
                           |
                           v
                    surface response
```

When the active catalog still offers an outer operation but the caller's live
card does not select it, the guard returns the existing structured
`delegated_capability_not_granted` evidence and an actionable consent block.
The block names the caller, protected resource, and exact outer operation. A
hosted agent's chat can open the focused card and submit only that operation;
an external OAuth/MCP client receives the same focused Connection Hub route.
Grant completion emits a passive `connections.consent.granted` event only for
the matching caller, resource, and operation. Approval does not replay the
refused call.

Conversation MCP demonstrates a plain managed operation with no connected
provider account. Its `conversations_export` tool requires
`conversations:read`, and the managed guard applies the current card/catalog
decision before the conversation export facade runs.

Productivity MCP demonstrates a plain account-backed MCP surface with no
named-service registration. The managed guard applies its resource and
per-tool grants first. Each tool then applies its declared `ToolClaimPolicy`
through `enforce_tool_requirements`, which resolves the card's account binding
and current provider claims before invoking Slack, mail, Sheets, Docs, or
LinkedIn code.

The named-services MCP door first passes the same managed resource/tool guard.
Its generic tool then decodes a namespace and inner operation. The common
dispatcher requires `NamedServiceAdmission` and applies the card's current
named-service selection under the active catalog before provider discovery.

## Other Named-Service Entrances

A native hosted-agent named-service tool can enter the common dispatcher
without crossing a managed MCP door. It constructs delegated admission inside
each `_call`; the trusted source bundle, agent, client, grantor, and actor form
the selector. A direct call resolves current Connection Hub state locally. A
relayed call carries the typed selector and actor to the target worker, which
validates both and resolves current state there.

Application authority is selected positively at a trusted named-service call
site. The source bundle and caller policy establish that authority.

```text
managed named-services MCP request     native hosted-agent tool     trusted application
guarded request-local snapshot         delegated selector           application admission
                  \                         |                         /
                   +------------------------+------------------------+
                                            |
                                            v
                                 common named-service dispatcher
                                 decoded namespace + operation
                                            |
                                            v
                                 one NamedServiceAdmission decision
```

## Reusing This System For Another Service

An app author can use the same delegated-authority system for a plain managed
REST or MCP surface:

1. Declare the REST or MCP surface with descriptor-owned managed auth.
2. Publish the protected resource, outer operations/tools, and required KDCube
   grants in the Connection Hub delegated catalog.
3. Let the managed guard bind the authenticated actor and resolve the exact
   current card/catalog decision for every request or tool invocation.
4. For a plain tool backed by connected accounts, declare its provider claims
   and call the shared account-enforcement helper before domain work.
5. For a named-service tool, pass explicit delegated or application admission
   into the common dispatcher; it owns the inner namespace/operation check.

The current built-in surfaces are worked examples. A bundle author can publish
the same pattern from an app-owned resource such as:

```text
example-crm@1-0/public/mcp/customers
  customers_search -> crm:read
  customers_update -> crm:write

managed guard
  -> exact caller card + active catalog
  -> resource/tool/grant check
  -> app-owned customer service
     or declared connected-account claim enforcement
```

The architecture applies equally to app-owned REST resources. The resource URL,
tool/operation names, grants, account requirements, and domain implementation
belong to that app and its descriptors.

An external backend that cannot sit behind a KDCube-managed door can instead
use direct protected-service admission. It sends the opaque delegated bearer,
the concrete resource/operation, and an independent replay-protected workload
proof to Connection Hub. Connection Hub authenticates the service before it
inspects the bearer, then invokes the same current card/catalog evaluator used
by managed guards. The backend receives pairwise service-scoped user and
caller-profile ids plus bounded authority, not internal Connection Hub ids or
a provider credential.

A remote MCP service that has no Connection Hub admission integration uses
the proxy path. Its owner registers the endpoint and connects with OAuth or an
explicit upstream credential. The connector projects accepted tools into that
owner's delegated catalog, and `remote_mcp_proxy` performs both admission and
the upstream tool call. The delegated caller sees only exact tools selected on
its card; the upstream credential stays in Connection Hub. The upstream OAuth
lifecycle is specified in
[User-Owned External MCP Proxy](../connection-hub-architecture.md#user-owned-external-mcp-proxy).

An OAuth-capable MCP client can obtain that caller card without a manually
issued bearer. Anonymous MCP and OAuth discovery advertise the protected proxy
resource and reveal no owner inventory. After platform login identifies the
grantor, the consent offer combines the active deployment catalog with that
grantor's active connector overlay. The grantor selects exact tools under exact
connector resources. Authorization-code exchange creates or updates one OAuth
card containing the proxy resource plus those selected connector resources and
their resource-qualified operations. Access-token use and refresh resolve that
same live card, so edits and revocation apply without distributing the remote
service credential or manually copying a Connection Hub bearer.

This is OAuth between the external caller and Connection Hub. Authentication
from Connection Hub to the external MCP server is the connector's upstream
credential path and is a separate capability.

```text
external caller -> opaque bearer -> external protected backend
                                      |
                         same bearer + signed service proof
                         concrete resource + operation
                                      |
                                      v
                         Connection Hub delegated_admission
                         service registration + replay check
                         current card + active catalog check
                                      |
                                      v
                         bounded allow or structured denial
```

The protected-service registry contains resource selectors only. Operations,
grants, and connected-account requirements stay in the active catalog. See
[Direct Protected-Service Admission](../connection-hub-architecture.md#direct-protected-service-admission)
for the wire contract and trust boundary.

The current and hypothetical surfaces compare as follows:

| Surface | Outer managed guard | Connected-account enforcement | Named-service admission |
| --- | --- | --- | --- |
| Conversation MCP | Resource + `conversations_export` + `conversations:read`. | None. | None. |
| Productivity MCP | Resource + each productivity tool's exact grants. | Per-tool `ToolClaimPolicy` and account broker. | None. |
| Named-services MCP | Resource + generic named-service MCP tools + `named_services:use`. | Provider requirements resolved for the inner operation. | Required for decoded namespace/operation. |
| Native hosted-agent named-service tool | Trusted agent selector and card. | Provider requirements resolved for the inner operation. | Required for each `_call`. |
| Custom app REST/MCP surface | App-owned resource, operations/tools, and grants. | Optional declared provider claims and shared account broker. | None when the app invokes its domain logic directly. |
| Registered external protected service | Signed service proof + opaque bearer evaluated by Connection Hub direct admission. | Optional card account-scope check; provider credential delivery remains a separate broker. | None unless the external service separately invokes named services. |
| User-owned external MCP service | Connection Hub proxy resolves exact card, connector, accepted tool descriptor, and invocation policy, then injects the upstream credential. | Expressed by the selected connector resource; the credential belongs to the connector owner. | None. |
| Custom named-service provider | Governed by whichever outer/native entrance calls it. | Optional provider requirements for its inner operations. | Required at the common dispatcher. |

## Exact Bearer Binding

A delegated-client bearer session retains the exact authenticated card binding:

```text
identity_authority.delegated_card_binding
  access_id          exact card selected at authentication
  client_id          delegated client identity
  grantor/delegate   identity relationship
  expires_at         authenticated binding metadata
```

This is session identity, not a cached grant. The `access_id` selects one card
when a user owns several delegated cards. The next invocation uses that exact
selector to resolve the current card revision and active catalog.

## Named-Service Direct And Relayed Dispatch

After execution enters the named-service subsystem, provider discovery answers
where the provider runs and `NamedServiceAdmission` answers whether this inner
invocation may reach it. The direct and relayed paths converge on the same local
provider registry:

```text
decoded request + admission input
                 |
                 v
        local platform caller available?
             /                 \
           yes                  no
            |                    |
            v                    v
 resolve/reuse admission     Data Bus request
 bind account scope          request + actor + typed selector
 local provider registry             |
            |                        v
            |                 provider-bundle worker
            |                 restore and bind actor
            |                 validate selector against actor
            |                 resolve current admission once
            |                 bind account scope
            |                 local provider registry
            |                        |
            +-----------+------------+
                        |
                        v
                 provider operation
                        |
                        v
                 structured response
```

The relay selector contains identifiers and provenance. The target owns the
card/catalog lookup and account binding. Raw cards, catalogs, account scopes,
and credentials stay in their owning trusted services.

The Connection Hub lookup for one card by `access_id` is Redis-first. Durable
storage participates when a validated serving projection must be restored, and
decides membership when a grantor's cards are listed. Relay retries use one
message id; the target records the completed outcome, redelivery returns that
outcome, and the provider executes once per message id.

## Invocation Boundaries

One invocation is the unit of authority:

- each managed REST request receives its own guarded decision;
- each MCP tool call, including each item in a batch, receives its own guarded
  decision;
- each plain account-backed tool resolves its declared account requirements for
  that tool call;
- each native agent tool `_call` receives a fresh decision;
- one relay request is validated and resolved once at its target;
- provider streaming is admitted once before the provider returns response
  metadata and its asynchronous bytes;
- consumption of an admitted byte stream uses that invocation's result;
  authorization and scope binding occur once at provider invocation.

The current relay transports ordinary request/reply results. Direct bridges own
provider byte-stream delivery.

## Once-Or-Always Invocation Policy

Card authority and invocation policy answer different questions:

```text
delegated card     may this caller use this operation?
invocation policy  may the already-granted operation be used once or repeatedly?
```

Invocation policy is stored separately from the card and is keyed by exact
`access_id + resource + surface + operation`, with an optional provider and
account selector. An account-specific policy overrides the operation's general
policy. For ordinary operations, no stored policy means `always`, preserving
existing granted cards. A protected-service operation configured as
request-bound requires an explicit policy and never inherits that fallback.

`once` starts with one available invocation. The caller supplies a stable
invocation id and a digest of the exact request. Connection Hub reserves that
id under the policy lock and consumes the one-use permit before dispatch. One
of concurrent new invocation ids wins. A later new id is denied. Reusing the
winning id with a different digest is also denied.

Request-bound `once` adds a short-lived permit containing the exact owner,
`access_id`, resource, operation, invocation id, request digest, card revision,
catalog revision, and expiry. The permit is created only after the browser
submits the signed approval ticket issued for that denied request. Reservation
and consumption happen under the same invocation-policy lock. Another
invocation under the same card and operation cannot take that permit.

The same-id replay boundary depends on who executes the operation:

| Provider mode | Connection Hub owns | Same id and digest |
| --- | --- | --- |
| User-owned external MCP proxy | Admission, upstream dispatch, and terminal tool result. | Returns the recorded result or terminal error without redispatching the upstream tool. |
| Direct protected-service admission | The bounded allow/deny decision only. | Returns the recorded admission decision. A state-changing provider must use the invocation id in its own effect-idempotency ledger. |

`always` permits new invocation ids repeatedly. It still records each supplied
id, so an uncertain retry with the same id and digest can receive the same
terminal result at a proxy-owned surface.

An exact operation grant and its initial `once` or `always` choice are committed
as one fail-closed cross-registry change. Connection Hub first writes a prepared
policy marker, then mutates the card, then commits the policy. Calls are denied
with a retryable policy-changing reason while the marker is prepared. Retrying
the same change id completes or replays the transaction; a second writer cannot
silently widen authority around it.

For request-bound operations, the denial URL carries a signed approval ticket
that also binds the protected service, caller profile, bounded display context,
and issue/expiry times. The browser treats it as opaque. The server verifies
the signature and compares every request and displayed field before changing
card or policy state. The ticket is the authenticated browser handoff; the
durable request permit is the authority consumed by the retried operation.

When an operation is absent from a current card, the structured denial names
the exact card, resource, and operation and offers `allow_once` and
`allow_always`. Approval adds only that operation and commits the selected
policy. An exhausted one-use policy returns the same choices without removing
the operation from the card.

## Changes And Revocation

Current state takes effect at the invocation boundary:

```text
card edit / revoke / expiry / new active catalog
                         |
                         v
             durable current state + Redis projection
                         |
                         v
next invocation resolves the new decision

existing bearer/session = identity + exact selector
authorization decision  = current state resolved for this invocation
```

A card edit narrows or widens the next invocation according to the newly
committed selection and current catalog. Revocation commits a durable revoked
revision, updates live serving state, and invalidates the applicable credential
or session records. An invocation that already received its singular decision
completes under that decision.

Catalog publication changes the deployment ceiling. A capability removed from
the active catalog becomes ineffective on the next invocation even while the
stored card preserves the old selection as drift evidence.

## Failure Semantics

| Condition | Result |
| --- | --- |
| Selector is missing or does not match the restored actor/session. | Structured admission denial before provider selection. |
| Exact card is expired, revoked, malformed, or identity-mismatched. | Structured delegated-authority denial. |
| Current active catalog or required card serving state cannot be obtained or validated. | `503 temporarily_unavailable`; a valid shared-state decision is required. |
| Card selected the capability but the active catalog removed it. | `403 delegated_capability_no_longer_available`. |
| Active catalog exposes the capability but the card did not select it. | Existing missing-grant or consent denial. |
| A one-use operation has no invocation id, is already consumed, or reuses an id with another request digest. | Structured invocation-policy denial with the exact recovery choices where applicable. |
| A request-bound operation has no exact permit, or its ticket/request/card/catalog facts moved. | Structured request-permit denial before dispatch; an authenticated matching browser handoff may create the exact permit. |
| A card/policy transaction is still prepared. | Retryable fail-closed denial until that exact change is completed. |
| An external MCP tool disappeared or its advertised descriptor changed. | Structured proxy denial before one-use authority is consumed. |
| Account binding or provider claims are incomplete. | Existing connection, account-selection, or claim-consent response. |

These outcomes preserve the distinction between identity failure, unavailable
shared authority state, deployment policy change, user-grant absence, and
connected-account consent.

## Source Map

- Once-or-always policy, reservation, and idempotency records:
  [`connection_hub.invocation_policy`](../../../products/connection-hub/packages/connection-hub/src/connection_hub/invocation_policy)
- User-owned connector, descriptor drift, endpoint policy, and proxy contracts:
  [`connection_hub.remote_mcp`](../../../products/connection-hub/packages/connection-hub/src/connection_hub/remote_mcp)
- KDCube-hosted direct-admission and remote-MCP surfaces:
  [`connection-hub@1-0/surfaces`](../../../products/connection-hub/apps/connection-hub@1-0/surfaces)
- Card/catalog durability and editor drift:
  [Delegated Access Cards](./delegated-cards.md)
- Reusable managed MCP doors and caller families:
  [Platform MCP Over Connection Hub](https://github.com/kdcube/kdcube/blob/main/app/ai-app/docs/sdk/solutions/mcp/platform-mcp-over-connection-hub-README.md)
- Managed MCP configuration and connected-account claims:
  [Authenticated MCP](https://github.com/kdcube/kdcube/blob/main/app/ai-app/docs/sdk/solutions/connections/authenticated-mcp/authenticated-mcp-README.md)
- Isolated supervisor and Data Bus round trip:
  [Named Services From An Isolated Runtime](https://github.com/kdcube/kdcube/blob/main/app/ai-app/docs/sdk/solutions/kdcube-services/named-services-from-isolated-runtime-README.md)
- Request, discovery, provider, and stream contracts:
  [Named-Service Integration](https://github.com/kdcube/kdcube/blob/main/app/ai-app/docs/sdk/namespace-services/integration-README.md)
- Cross-runtime actor and policy restoration:
  [Cross-Runtime Context](https://github.com/kdcube/kdcube/blob/main/app/ai-app/docs/runtime/cross-runtime-context-README.md)
- Trust boundaries and credential ownership:
  [Security And Trust Model](https://github.com/kdcube/kdcube/blob/main/app/ai-app/docs/arch/security-and-trust-model-README.md)
