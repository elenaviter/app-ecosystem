---
id: connection-hub/package/delegated-mcp-gateway
title: Delegated MCP Gateway
summary: Defines the portable aggregate MCP gateway that projects every compatible resource on one live delegated card through qualified tools and exact provider dispatch.
status: active-local-host-integration-accepted
tags: [connection-hub, mcp, delegated-access, gateway, providers, authorization]
keywords: [delegated MCP gateway, aggregate tools list, provider registry, caller-self discovery, stable tool names, live card]
updated_at: 2026-09-04
see_also:
  - ./delegated-authority-and-admission.md
  - ./delegated-cards.md
  - ../connection-hub-architecture.md
  - ../testing/end-to-end-acceptance.md
---

# Delegated MCP Gateway

The delegated MCP gateway is one MCP surface over all MCP-compatible resources
granted on one caller's current Connection Hub card. Its portable implementation
lives in `connection_hub.delegated_gateway`. The KDCube-hosted request builder
lives in the Connection Hub app's `surfaces/delegated_gateway.py`.

**Contract signal: `GATEWAY-CONTRACT-READY`.** The provider and wire contracts,
the pure adapter over the published Card read model, both provider adapters,
the hosted MCP and caller-self API surfaces, invocation-policy and audit
adapters, requestable-resource reader, and descriptor registration are
implemented and tested. A staged local KDCube acceptance has exercised one
resident Card containing both a managed KDCube MCP resource and a user-owned
external MCP resource through one Gateway. Existing `remote_mcp_proxy` clients
retain their external-only endpoint and established tool names.

## Boundary

```text
authenticated caller
        |
        v
host caller resolver ------------ bearer/session verification belongs to host
        |
        v
DelegatedMCPGateway
  read current Card view -------- every list, call, and describe request
  select exact provider --------- one resource kind has at most one provider
  compare accepted/current ------ operation descriptor digest, not display name
  consume invocation policy ----- immediately before dispatch
  audit non-secret evidence
        |
        +----------------------+----------------------+
        |                                             |
        v                                             v
ExternalRemoteMCPProvider                  ManagedKDCubeMCPProvider
existing connector + proxy resolve        injected in-runtime host port
server-side provider credential           managed surface admission retained
        |                                             |
        +----------------------+----------------------+
                               |
                               v
                      bounded MCP result
```

The gateway is stateless with respect to card authority. A tools list is a
discovery snapshot, not an authorization token. A later `tools/call` rebuilds
the name index from the current card and rechecks the exact provider descriptor
before any policy is consumed or effect is dispatched.

## Published Card Read Contract

The host adapter supplies a `DelegatedCardReader`. Its non-secret current view
must contain these fields:

| Record | Required fields |
| --- | --- |
| Caller | caller type, stable caller profile id, stable access id, client id when present, explicit capabilities, optional resource ceiling |
| Card | caller type, caller profile id, access id, monotonically increasing card revision, active/revoked status, expiry, source, identity scope, grantor subject for internal owner checks |
| Resource | exact stable resource id, provider-selecting kind and optional provider id, display label, callable endpoint relation, identity scope, active/disabled state, grants, resource-qualified operations |
| Accepted descriptor | provider revision, full descriptor digest, digest for every selected operation |
| Invocation policy | operation, Once/Always mode, public state, policy revision, public remaining count |
| Recovery | fixed reason code and an HTTPS or host-relative owner route |
| Provider metadata | non-secret locator metadata needed by that provider; credential-shaped keys are rejected |

The Card read adapter must return the card identified by all three caller
coordinates: caller type, caller profile id, and access id. A mismatch, absent
card, revocation, or expiry is a denial. The portable gateway does not read Card
persistence and does not reproduce Card identity hashing.

`adapt_card_view()` is the pure adapter over Card's published
`delegated_credentials.cards.read_model.DelegatedCardView`. It derives a
resident caller profile coordinate from `ResidentCallerProfile.client_id` and
uses `client_id` for manual and OAuth caller families; `access_id` remains the
independent card coordinate in every case. It maps Card's accepted resource and
operation digests and validates that every public invocation policy names the
same resource and operation.

Card storage intentionally does not own deployment route metadata. The adapter
therefore requires a `GatewayResourceMetadata` resolver for each resource. The
host supplies the general endpoint relation, fixed recovery links, and any
non-secret managed-surface locator. It supplies Card discovery capabilities
explicitly as well. Missing accepted operation evidence, a mismatched provider,
or a policy bound to another authority fails closed. A changed resource remains
eligible for per-operation live checks; removed and unknown resources adapt as
disabled.

The exact accepted descriptor identity used for routing is a canonical digest
of:

```json
{
  "descriptor_digest": "<accepted resource digest>",
  "descriptor_revision": "<accepted provider revision>",
  "operation": "<upstream operation>",
  "operation_digest": "<accepted operation digest>"
}
```

An operation is callable only when the current provider advertises the same
operation digest. Added operations are not granted. Removed or changed
operations remain unavailable until a new Card revision accepts them.

## Provider Contract

`DelegatedMCPResourceProvider` is host-neutral:

```text
provider_id
resource_kinds
current_descriptor(context) -> ProviderDescriptor
list_tools(context) -> ProviderTool[]
admit_call(context, operation, arguments, invocation_id) -> ProviderCallAdmission
call_tool(context, operation, arguments, invocation_id) -> ProviderCallResult
```

`GatewayProviderContext` carries the authenticated caller, current card, and
one exact resource. Providers perform resource-specific readiness and effect
execution. They do not grant Card authority, consume the gateway invocation
policy, or return provider credentials. `ProviderCallAdmission` is a
credential-free allow/deny record. The gateway runs it after current descriptor
intersection and before auditing and consuming Once/Always policy. Unknown
provider denial reasons collapse to `provider_admission_denied`.

`DelegatedMCPProviderRegistry` rejects duplicate provider ids and overlapping
resource kinds. Unknown resource kinds fail closed. Listing isolates one
provider failure, so an unavailable connector cannot erase or widen another
provider's inventory.

### External MCP

`ExternalRemoteMCPProvider` owns the `remote_mcp` kind. It uses the existing
owner-scoped connector service for current discovery and upstream execution.
Its aggregate listing delegates to `RemoteMCPProxy.list_authorized()`, retaining
the existing connector grant, operation, and live-schema filters. Its provider
admission calls `RemoteMCPProxy.resolve()` before the aggregate invocation
policy is consumed. Dispatch resolves through the proxy again immediately
before invoking the connector service, so a readiness or schema change in that
interval fails closed. The aggregate gateway owns its invocation policy, so the
adapter invokes the connector service directly after the second resolve.

The adapter never places the upstream bearer, OAuth session, or arbitrary
endpoint in a gateway tool schema or result.

### Managed KDCube MCP

`ManagedKDCubeMCPProvider` depends only on `ManagedKDCubeMCPHost`. The host
implementation must:

- resolve a non-secret managed surface locator from the exact resource entry;
- retain the direct surface's authentication and admission checks;
- receive caller, card, resource, operation, arguments, and invocation id;
- implement a credential-free `admit_call` preflight before outer policy use;
- execute through a trusted in-runtime port rather than public-ingress HTTP
  recursion;
- return a bounded `ProviderCallResult` without a provider credential.

The gateway consumes the outer invocation policy. The managed host performs
the provider effect and preserves any independent admission boundary owned by
the direct surface. A bundle-authenticated application surface is not projected
as user-delegated merely because it speaks MCP.

## Qualified Tool Names

Aggregate names are derived only from stable authority identity:

```text
ch_<resource-kind>_<resource-id-sha256[0:16]>
  __<operation-slug>_<route-identity-sha256[0:16]>
```

The route identity contains the exact resource id, upstream operation, and
accepted descriptor identity. Labels and endpoint origins do not affect the
name. Equal upstream operation names on different resources remain distinct.
The request-local `QualifiedToolNameIndex` provides exact reverse routing and
rejects a hash collision rather than choosing one provider.

The existing `remote_mcp_proxy` naming formula remains unchanged. Aggregate
gateway names are a separate namespace and do not silently migrate current
clients.

## List And Call

`tools/list` performs:

1. Read and bind the current active Card.
2. Iterate active resources in stable id order.
3. Resolve one provider by exact resource kind.
4. Resolve current provider readiness and descriptor.
5. Intersect selected operations with current tool descriptors.
6. Publish qualified tools plus `connection_hub_access_describe`.
7. Sort by the final MCP tool name.

The KDCube host constructs a stateless request-scoped MCP server, so normal
client polling observes Card changes. With the current MCP SDK, the resulting
2026-07-28 server advertises `tools.listChanged: true` through its
`subscriptions/listen` capability. Production registration must retain that
transport behavior and the request-scoped rebuild.

`tools/call` performs:

1. Validate and bound JSON arguments and the invocation id.
2. Read the current active Card.
3. Rebuild the accepted name index and resolve the exact resource/operation.
4. Re-resolve provider readiness and current operation descriptor.
5. Run the provider's credential-free resource admission check.
6. Record current-authority audit evidence.
7. Begin the current Once/Always policy for the canonical request digest.
8. Let the provider retain its final resource-specific check and dispatch the
   effect exactly once.
9. Complete replay state and record a fixed, non-secret outcome.
10. Return bounded MCP content and structured metadata.

The canonical request digest binds the resource, operation, accepted descriptor
identity, and arguments. Reusing an invocation id with another request is a
conflict. A completed invocation replays its stored result without another
provider effect.

If a client disconnects after dispatch, `asyncio.shield` lets the provider
effect and policy completion finish in a retained task. Service shutdown may
still cancel that task; in that case the policy reservation remains incomplete
instead of claiming a result. The host must drain requests during shutdown and
the provider must honor the same invocation id at any independent effect
ledger it owns.

## Caller-Self Discovery

Both the MCP meta-tool and the bearer-authenticated API return
`connection_hub.delegated_gateway.access.v1`. The granted view contains:

```text
caller
  type, profile_id, access_id
card
  revision, status, expires_at, expired, source, identity_scope
resources[]
  stable id, kind, provider id, label, endpoint relation, identity scope, state
  grants, operations
  accepted descriptor revision/digest/operation digests
  current descriptor revision/digest/state
  public invocation policies
  fixed unavailable reason and recovery links
requestable_discovery
requestable_resources[]
```

The grantor subject is used internally for owner intersection and is not
returned. Access tokens, provider credentials, credential references, arbitrary
provider exception text, and owner-private requestable inventory are absent.

Requestable resources are returned only when both caller profile and Card carry
`discover_requestable`. The gateway then intersects the reader's candidates
with grantor ownership, identity scope, allowed caller profile ids, and the
caller's optional resource ceiling. Discovery never mutates a Card or consumes
an invocation policy.

## Bounds And Fixed Failures

Defaults are 256 KiB for arguments, 1 MiB for a complete provider result, and
512 listed tools including the meta-tool. Hosts may configure smaller positive
limits when constructing the gateway.

Card, descriptor, policy, audit, collision, and provider-readiness denials use
fixed reason codes. Arbitrary provider and host exception messages are not
copied to MCP responses, audit events, or caller-self output. A provider effect
failure completes the invocation with a fixed error result so retrying the same
invocation id cannot create a second effect.

## Hosted KDCube Integration

The hosted Connection Hub application supplies the host-owned adapters:

1. authenticated Card lookup through `adapt_card_view()`, including resource
   metadata and requestable-discovery capabilities;
2. durable Once/Always and replay handling through `GatewayInvocationPolicy`;
3. non-secret operational audit;
4. external Remote MCP and managed KDCube MCP providers;
5. request-scoped caller resolution without exposing the bearer;
6. the `delegated_mcp_gateway` MCP surface;
7. the `delegated_mcp_gateway_access` caller-self API;
8. OAuth, MCP, delegated-catalog, and application descriptor registration.

KDCube projects the Gateway per turn for resident agents. One resident caller
profile is identified by grantor, application, and agent id and resolves one
stable Card and `access_id`. Every compatible resource selected on that Card
appears through one credential-free Gateway binding. Adding or removing a
resource changes the Card revision and next request; it does not create a
second resident Card.

Local staged acceptance covers both provider kinds, direct managed-surface
admission, caller-self discovery, qualified listing and calls, Workspace
projection, live narrowing, revocation, and application-process restart.
External OAuth clients retain independent Cards because their connector and
OAuth session are independent caller profiles.

Remaining release gates are the visible human Workspace workflow, the native
client platform matrix, and operator-initiated package and ECS deployment
acceptance. Follow [Connection Hub And Governed MCP End-To-End Acceptance](../testing/end-to-end-acceptance.md).
