---
id: connection-hub-interface
title: Connection Hub Interface
summary: Documents the authenticated operations, public proof/OAuth/admission routes, user-owned external MCP proxy, named-service provider, and browser widget exposed by the Connection Hub app.
tags:
  - connection-hub
  - connection-hub
  - interface
keywords:
  - Connection Hub operations
  - OAuth routes
  - named-service provider
  - external MCP proxy
  - invocation policy
updated_at: 2026-09-02
see_also:
  - ./connection-hub.openapi.yaml
  - ../../../../../docs/connection-hub/connection-hub-architecture.md
  - ../../../../../docs/connection-hub/frontend/application/README.md
---

# Connection Hub Interface

`connection-hub@1-0` exposes authenticated `operations` aliases, public
proof/auth/OAuth/admission routes, one `connections` named-service provider,
one delegated external-MCP proxy, and one widget.
The OpenAPI file
[connection-hub.openapi.yaml](connection-hub.openapi.yaml) documents the
operations and callback routes in detail; this README is the human contract.

Every POST `operations` body is wrapped as `{ "data": { ... } }` by frontend
callers. Responses are platform-wrapped; unwrap the property named by the
operation alias.

The semantic distinctions, storage authorities, and trust boundaries are
canonical in the
[Connection Hub architecture](../../../../../docs/connection-hub/connection-hub-architecture.md).

## Browser surfaces

The Connections settings widget is a separate React/Redux browser app served from
`ui/widgets/connections` and controlled by `ui.widgets.connections_settings`:

```text
/api/integrations/bundles/{tenant}/{project}/connection-hub@1-0/widgets/connections_settings
```

The widget reads/writes through the operations below. It shows connection
edges, connected accounts, delegated caller cards, invocation policy, and
user-owned external MCP connectors while keeping their semantics separate. It
never returns provider or connector credentials; it only submits them into
Hub-owned server-side stores.

## Connection-edge vs delegated account contract

```text
connection edge
  provider + provider_subject -> platform_user_id
  purpose: prove and route the platform principal

delegated account connection
  platform_user_id + provider account -> token/claim
  purpose: let automation act for the user
```

`identity_resolve` returns a principal envelope. The `role_resolution` field is
reserved for a platform principal/role resolver. The app's configured
`identity.role_bindings` mode is a local fixture only; consumers must not treat
Connection Hub as the final role authority.

The current manual connection-edge form is a development/onboarding fixture. A
production auth bridge must first verify the external proof (OAuth profile,
Telegram login signature, signed app webhook, etc.) and only then call
`identity_resolve` for that verified `provider + provider_subject`.

## API aliases (operations route)

All are authenticated (`PlatformAuth`) and visibility-gated by
`visibility.api.<alias>.{user_types,roles}`.

| Alias | Method | Route | Purpose |
| --- | --- | --- | --- |
| `named_service` | POST | operations | Serves the whole `connections` named-service contract (provider/about/capabilities, object list/get/action/resolve, `connection.get_token`, start/disconnect). |
| `connection_edges_list` | GET | operations | List external identities linked to the current platform user. |
| `connection_edge_upsert` | POST | operations | Link a verified external identity to the current platform user. User-facing route; does not grant roles. |
| `connection_edge_remove` | POST | operations | Remove one external connection edge from the current platform user. |
| `connection_edge_challenge_create` | POST | operations | Create a short-lived one-time proof challenge for the current platform user. |
| `connection_edge_challenge_status` | POST | operations | Read a proof challenge without claiming it. Platform-first challenges are limited to their platform user; provider-first pending challenges can be previewed by the currently signed-in platform user before explicit confirmation. |
| `identity_resolve` | POST | operations | Resolve a verified external identity (`provider`, `provider_subject`) to a platform principal envelope. |
| `identity_family_resolve` | POST | operations | Resolve the current actor/platform user to the linked identity family: platform authority identity, provider/integration identities, and canonical user ids for aggregation. |
| `delegated_identity_scope_resolve` | POST | operations | Resolve a verified delegated credential envelope to the grantor user ids allowed by its delegation edge and identity scope. |
| `authenticators_list` | GET | operations | List Connection Hub authenticator modules/configured rows and secret-reference status. Secret values are never returned. |
| `authenticators_upsert` | POST | operations | Create/update a Postgres-backed authenticator metadata row. Accepts authenticator selector metadata, `role_providing`, and `secret_ref`; rejects secret values. |
| `authenticators_remove` | POST | operations | Soft-delete a Postgres-backed authenticator metadata row. Descriptor-defined rows are not removed through this API. |
| `dcr_allowlist_get` | GET | operations | Admin: read the OAuth dynamic-client-registration redirect allowlist (configured, effective, defaults). |
| `dcr_allowlist_set` | POST | operations | Admin: replace the DCR redirect allowlist. Absolute URIs only; `http` restricted to loopback hosts; empty list falls back to defaults. |
| `delegated_to_kdcube_catalog` | GET | operations | Catalog of providers → connector apps → user accounts, with connected/configured flags. |
| `delegated_to_kdcube_start_oauth` | POST | operations | Begin OAuth for `provider` + `connector_app_id`; optional selected claims. Returns an authorize URL. |
| `delegated_to_kdcube_connect_credential` | POST | operations | Store a non-OAuth credential, such as an iCloud app-specific password, as a user-scoped connected account delegated to KDCube. |
| `delegated_to_kdcube_disconnect` | POST | operations | Disconnect a user account (`provider`, `account_id`). |
| `delegated_to_kdcube_resolve` | POST | operations | Resolve a delegated-to-KDCube credential for code acting on behalf of the current authenticated user. Secret values are returned only to server-side callers. |
| `delegated_access_list` | GET | operations | List automation credentials and the configured resource/grant catalog. Named-service resources include their descriptor-backed namespace operation tree and provider-declared connected-account requirements. |
| `delegated_access_create` | POST | operations | Mint a short-lived KDCube automation bearer bounded by exact `resource_grants`, `resource_operations`, and optional `named_service_operations` selections. The flat top-level `operations` list is derived for compatibility. |
| `delegated_access_update` | POST | operations | Replace or preserve the selected dimensions of an existing owner-scoped card in place. The credential remains bound to the same `access_id`. |
| `delegated_agent_grant_create` | POST | operations | Merge or replace exact authority on a hosted-agent or existing external-client card. An operation-recovery submission may atomically add one operation and set it to `once` or `always`. |
| `delegated_invocation_policy_set` | POST | operations | Set `once` or `always` for one already-granted resource operation, optionally scoped to a selected provider account. |
| `delegated_access_revoke` | POST | operations | Revoke one automation or OAuth delegated-client grant owned by the current user. |
| `remote_mcp_connectors_list` | GET | operations | List the current user's external MCP connectors and accepted/pending descriptor metadata without secret values. |
| `remote_mcp_connector_create` | POST | operations | Validate an endpoint, store an optional bearer/header credential server-side, discover tools, and create the first accepted connector revision. |
| `remote_mcp_connector_start_oauth` | POST | operations | Discover an upstream MCP authorization server, select its advertised client-registration method, create a single-use PKCE transaction, and return the browser authorization URL. |
| `remote_mcp_connector_refresh` | POST | operations | Rediscover tools under an optimistic connector revision and record descriptor drift without accepting it. |
| `remote_mcp_connector_accept_descriptor` | POST | operations | Promote the pending discovered descriptor to the accepted revision. Newly accepted tools remain ungranted on caller cards. |
| `remote_mcp_connector_set_enabled` | POST | operations | Enable or disable the connector under an optimistic revision precondition. |
| `remote_mcp_connector_update_credential` | POST | operations | Replace the server-side upstream credential, rediscover, and record any resulting descriptor drift. |
| `remote_mcp_connector_delete` | POST | operations | Commit a deleted connector revision and remove its upstream credential. |
| `email_accounts_status` | GET | operations | Older iCloud account status route retained only for the older email integration surface. |
| `email_connect_app_password` | POST | operations | Older iCloud app-password route retained only for the older email integration surface. |
| `email_disconnect_account` | POST | operations | Older iCloud disconnect route retained only for the older email integration surface. |
| `connections_settings` | GET | operations | Thin widget data alias (also the `@ui_widget` alias). |

> Gmail, Slack, and iCloud-style app-password accounts are now configured under
> `connections.delegated_to_kdcube`. OAuth providers use
> `delegated_to_kdcube_start_oauth` and the public
> `delegated_to_kdcube_oauth_callback`; non-OAuth providers use
> `delegated_to_kdcube_connect_credential`.

### Public OAuth routes

A browser redirect target, not a JSON op — reached by the external provider after
the user authorizes. One delegated-to-KDCube callback is shared by OAuth
providers such as Gmail and Slack:

| Alias | Method | Route | Redirect URI to register |
| --- | --- | --- | --- |
| `delegated_to_kdcube_oauth_callback` | GET | public | `…/connection-hub@1-0/public/delegated_to_kdcube_oauth_callback` |
| `remote_mcp_oauth_callback` | GET | public | `…/connection-hub@1-0/public/remote_mcp_oauth_callback` |
| `remote_mcp_oauth_client_metadata` | GET | public | Client metadata document used as the URL-based OAuth client id when the authorization server advertises that method. |

It accepts `code`, `state`, `error` and completes the hub-owned flow, validating
`state` with `connections.delegated_to_kdcube.oauth_state_secret`. The
provider + connector app are read from the signed `state`.

The remote-MCP callback consumes a random single-use state. Its durable
pointer contains only a digest, owner, secret reference, and expiry. PKCE,
dynamic-client credentials, and discovered token endpoints remain in the
referenced user secret until the callback claims it. The callback exchanges
the code and creates the owner connector without returning OAuth tokens to the
browser.

### User-owned external MCP proxy

The connector operations above manage remote streamable-HTTP MCP services for
the signed-in owner. The public MCP surface is:

```text
/api/integrations/bundles/{tenant}/{project}/connection-hub@1-0/public/mcp/remote_mcp_proxy
```

It uses managed delegated-client authentication. `tools/list` returns only
tools selected on the caller's exact live card. `tools/call` resolves the card,
current connector state, accepted and observed descriptor of the exact tool,
and current invocation policy before Connection Hub injects the owner-held
upstream credential and dispatches the call.

Connector records expose `credential_mode`, optional non-secret header name,
and `credential_present`. They never expose `credential_value` or the internal
credential reference. Endpoint policy is descriptor-owned under
`connections.remote_mcp.outbound`; a user-supplied URL cannot enable HTTP or
private-network access for itself.

OAuth mode follows MCP protected-resource and authorization-server discovery
and PKCE. It can use the public Client ID Metadata Document, dynamic client
registration, or a client the owner registered in the provider console. The
provider-console client id and optional secret enter only the authenticated
start operation and are retained with the connector's server-side OAuth
credential. Browser responses expose only the non-secret client-source marker.
Expiring access tokens refresh under a connector-scoped cross-worker lock.
Ordinary reconnect reuses a provider-console client; explicit replacement can
select another provider-console client or automatic registration. Caller OAuth
into `remote_mcp_proxy` remains an independent credential and card.

A tools/call request may carry a stable id in MCP metadata at
`connection_hub/invocation_id`. The proxy derives a digest from the upstream
tool name and arguments. Standard clients that omit the metadata receive a new
generated id for that call. A `once` policy consumes one new id. Repeating an
explicit id with the same arguments replays the stored terminal result without
calling the upstream server again; changing the arguments under that id is
denied.

### Direct protected-service admission

`delegated_admission` lets a registered backend that is not behind a KDCube
REST/MCP door evaluate one opaque delegated bearer against current authority.
It is disabled by default.

| Alias | Method | Route | Purpose |
| --- | --- | --- | --- |
| `delegated_admission` | POST | public | Authenticate the protected service, reject replay, resolve the opaque bearer to its current card, intersect it with the active catalog and optional account scope, then return a bounded allow/deny decision. |

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
  "request_digest": "<64-lowercase-hex-sha256>",
  "account": {
    "provider_id": "salesforce",
    "account_id": "account-17",
    "claims": ["contacts:read"]
  }
}
```

The service signature binds its id, timestamp, nonce, SHA-256 of the bearer,
and SHA-256 of the canonical semantic request. Service secrets contain at
least 32 bytes. The default timestamp window is 300 seconds and the default
single-use nonce lifetime is 600 seconds.

The response carries `connection_hub.delegated_admission.v1`, a correlation
id, a service-scoped pairwise user id, a separate service-scoped pairwise
caller-profile id, effective resource/operation/grants, optional account scope,
card/catalog provenance, expiry, and invocation-policy state when one exists.
It never returns the internal platform user id, raw caller client id, raw card
access id, bearer, identity-family scope, or provider credential.

`invocation_id` and `request_digest` are optional together and required for a
`once` policy. Connection Hub records and may replay the admission decision. A
state-changing protected service remains responsible for applying its own
domain effect once under that invocation id.

The complete signing input, response schema, registration shape, and trust
boundary are in
[Direct Protected-Service Admission](../../../../../docs/connection-hub/connection-hub-architecture.md#direct-protected-service-admission).

### Public Telegram proof route

Telegram proof routes are not platform-login routes. They validate Telegram
Mini App `initData`; the platform user is supplied only by an authenticated
KDCube session.

There are two directions:

```text
Platform-first:
  KDCube session creates challenge -> host-specific provider proof surface completes it.

Telegram-first:
  host-specific provider surface creates provider proof -> KDCube session claims it.
```

| Alias | Method | Route | Purpose |
| --- | --- | --- | --- |
| `telegram_connection_edge_start` | POST | public | Validate Telegram Mini App `initData`, create a pending provider proof, and return `platform_claim_url`. |
| `telegram_connection_edge_status` | GET | public | Validate Telegram Mini App `initData` and report whether that Telegram subject is already linked. |
| `telegram_connection_edge_remove` | POST | public | Validate Telegram Mini App `initData` and unlink that Telegram subject from its platform user. |
| `telegram_connection_edge_complete` | POST | public | Validate Telegram Mini App `initData` and complete a pending Telegram connection-edge challenge. |
| `federated_data_bus_claim` | POST | public | Validate promoted request auth context and issue a short-lived Socket.IO token for the Connection Hub widget live channel. |
| `request_authenticate` | POST | public | Gateway/app selector endpoint: authenticate a request envelope through configured authenticators and return linked authority. |

The caller must send signed Telegram Mini App initData in
`X-Telegram-Init-Data`. KDCube-controlled callers should also send the
non-secret selector headers `X-KDCube-Auth-Authority-ID: <authority_id>` and
`X-KDCube-Auth-Authenticator-ID: <authenticator_id>`.
`telegram_connection_edge_complete` also requires body
`{ "data": { "challenge_id": "..." } }`.

`request_authenticate` is the generic request-authenticator operation. It is
not user-facing login UI. Gateway/middleware and app/channel handlers call it
with a normalized request envelope. The provider proof implementations are
Connection Hub modules: they can read Connection Hub connection-edge storage,
app config, and app secrets, while callers stay provider-neutral.

Authenticator admin APIs store metadata only. A row can say
`id: telegram.support` and
`secret_ref: identity.authenticators.telegram_support.bot_token`, but the token itself must be
stored through `bundles.secrets.yaml` or the configured bundle secrets provider.
Posting fields such as `secret_value`, `bot_token`, `client_secret`,
`signing_secret`, or `api_key` is rejected. `role_providing` is false for
linked external providers such as Telegram; platform roles come from the linked
platform principal.

```json
{
  "data": {
    "request": {
      "method": "POST",
      "path": "/api/integrations/...",
      "headers": {
        "x-telegram-init-data": "<Telegram.WebApp.initData>"
      },
      "query": {},
      "cookies": {}
    }
  }
}
```

Example response:

```json
{
  "ok": true,
  "authenticated": true,
  "provider": "telegram",
  "provider_subject": "314062490",
  "actor_user_id": "telegram_314062490",
  "platform_user_id": "02e...",
  "identity_authority": {
    "actor_user_id": "telegram_314062490",
    "platform_user_id": "02e...",
    "economics_user_id": "02e...",
    "platform_roles": ["kdcube:role:super-admin"],
    "budget_bypass": true
  }
}
```

## Request payload shapes

`delegated_to_kdcube_start_oauth`:

```json
{
  "data": {
    "provider_id": "slack",
    "connector_app_id": "demo",
    "claims": ["slack:search"],
    "return_hint": ""
  }
}
```

`delegated_to_kdcube_disconnect`:

```json
{ "data": { "provider": "slack", "account_id": "<workspace_account_id>" } }
```

`delegated_to_kdcube_connect_credential` (iCloud):

```json
{
  "data": {
    "provider_id": "icloud_mail",
    "connector_app_id": "app_password",
    "claims": ["email:read", "email:send"],
    "email": "user@icloud.com",
    "external_subject": "user@icloud.com",
    "display_name": "User",
    "credential": {
      "app_password": "<apple-app-specific-password>"
    }
  }
}
```

Provider claim policy is configured on Connection Hub. Application tool claim
policy is configured next to the tool that needs the external account. A tool
references the provider and connector app directly; it does not need an
intermediate capability id.

```yaml
connections:
  delegated_to_kdcube:
    providers:
      slack:
        claims:
          slack:post:
            label: Post to Slack
            provider_scopes: [chat:write]
        connector_apps:
          demo:
            allowed_claims: [slack:post]
```

Application bundle/tool config owns the tool boundary:

```yaml
tools:
  report.post_to_slack:
    label: Post report to Slack
    connections:
      delegated_to_kdcube:
        connected_accounts:
          - provider_id: slack
            connector_app_id: demo
            claims: [slack:post]
```

Server-side application code parses the local tool config and passes that policy
to `DelegatedToKdcubeClient.ensure_tool_claims(...)`:

```python
from connection_hub.delegated_to_kdcube import (
    ToolClaimPolicy,
)

policy = ToolClaimPolicy.from_tool_config("report.post_to_slack", tool_config)
result = await delegated_to_kdcube_client.ensure_tool_claims(policy=policy)
```

Connection Hub can derive a "which tools ask for which provider claims" catalog
by scanning bundle descriptors, but that catalog is derived metadata. The source
of truth remains the application bundle/tool definition.

Named services do not need a second per-tool declaration. Their provider spec
publishes `metadata.connected_accounts`, and the delegated MCP resource owns the
allowed namespace/tool/grant tree under `resources[].named_services`. The
automation-access list projects those two existing declarations directly:

```text
delegated_access_list.resources[].named_services[]
  tools / operations / grants       <- delegated MCP resource descriptor
  connected_accounts               <- named-service provider discovery metadata
```

The descriptor and provider projection is read-only source data, but its
existing namespace operations are selectable when creating automation access.
The request sends `named_service_operations[resource][namespace][]`; the
backend validates that selection and stores a narrowed copy of the same
`named_services` policy in the token's `GrantStore` record. Action variants such
as `object.action.post_message` are selectable only when the descriptor declares
those exact identifiers; Connection Hub does not infer variants from provider
code or action payloads. Provider credentials are never copied into the
automation bearer. The widget uses the existing provider/connector/claims deep
link to continue in **Delegated to KDCube**.

`delegated_to_kdcube_resolve` resolves one explicit provider claim:

```json
{
  "data": {
    "provider_id": "slack",
    "connector_app_id": "demo",
    "claim": "slack:post"
  }
}
```

`named_service` (standard envelope; serves the whole `connections` contract):

```json
{
  "data": {
    "operation": "connection.get_token",
    "namespace": "connections",
    "payload": { "provider": "slack" }
  }
}
```

`connection_edge_upsert`:

```json
{
  "data": {
    "provider": "google",
    "provider_subject": "user@example.com",
    "label": "Google account"
  }
}
```

`connection_edge_challenge_create`:

```json
{ "data": { "provider": "telegram" } }
```

Example response:

```json
{
  "ok": true,
  "challenge": {
    "challenge_id": "one-time-token",
    "provider": "telegram",
    "platform_user_id": "02e...",
    "status": "pending",
    "expires_at": 1780000000
  }
}
```

This operation creates a server-side challenge only. The host surface that owns a
specific Telegram Mini App is responsible for opening that Mini App and carrying
the challenge id; Connection Hub does not derive a generic Telegram destination.

`telegram_connection_edge_start`:

```http
POST /api/integrations/bundles/{tenant}/{project}/connection-hub@1-0/public/telegram_connection_edge_start
X-Telegram-Init-Data: <Telegram.WebApp.initData>
Content-Type: application/json

{ "data": {} }
```

Example response:

```json
{
  "ok": true,
  "provider": "telegram",
  "provider_subject": "314062490",
  "challenge": {
    "challenge_id": "one-time-token",
    "provider": "telegram",
    "provider_subject": "314062490",
    "status": "pending_platform_claim",
    "expires_at": 1780000000
  },
  "platform_claim_url": "https://.../public/widgets/connections_settings?claim_challenge=one-time-token"
}
```

When the caller wants a no-poll completion signal, include the Connection Hub
live session id returned by `federated_data_bus_claim`:

```json
{ "data": { "live_event_session_id": "socket-session-id" } }
```

Connection Hub stores that session id on the challenge and emits
`connection_hub.edge.changed` to it after
`connection_edge_challenge_claim` succeeds.

`federated_data_bus_claim`:

```http
POST /api/integrations/bundles/{tenant}/{project}/connection-hub@1-0/public/federated_data_bus_claim
X-Telegram-Init-Data: <Telegram.WebApp.initData>
X-KDCube-Auth-Authority-ID: telegram.kdcube_ref
X-KDCube-Auth-Authenticator-ID: telegram.kdcube_ref.init_data
Content-Type: application/json

{ "data": {} }
```

Example response:

```json
{
  "ok": true,
  "schema": "kdcube.federated_token_claim.v1",
  "federated_token": "kst-fed...",
  "session_id": "federated-session-id",
  "expires_at": 1780000000,
  "bundle_id": "connection-hub@1-0"
}
```

`connection_edge_challenge_claim`:

```http
POST /api/integrations/bundles/{tenant}/{project}/connection-hub@1-0/operations/connection_edge_challenge_claim
Content-Type: application/json

{ "data": { "challenge_id": "one-time-token", "confirmed": true } }
```

This route requires the user to be authenticated in KDCube and requires
`confirmed=true`. The browser claim page must call
`connection_edge_challenge_status` first, show the Telegram identity and current
KDCube user, and only then call this route after the user explicitly confirms.
It links the verified Telegram identity from the pending challenge to the
current platform user.

`telegram_connection_edge_complete`:

```http
POST /api/integrations/bundles/{tenant}/{project}/connection-hub@1-0/public/telegram_connection_edge_complete
X-Telegram-Init-Data: <Telegram.WebApp.initData>
Content-Type: application/json

{ "data": { "challenge_id": "one-time-token" } }
```

`identity_resolve`:

```json
{
  "data": {
    "provider": "telegram",
    "provider_subject": "314062490"
  }
}
```

Example response:

```json
{
  "ok": true,
  "connection_edge": {
    "provider": "telegram",
    "provider_subject": "314062490",
    "platform_user_id": "02e..."
  },
  "principal": {
    "platform_user_id": "02e...",
    "roles": [],
    "permissions": [],
    "role_resolution": {
      "status": "platform_resolver_not_wired",
      "source": "platform.principal_role_resolver"
    }
  }
}
```

`identity_family_resolve`:

```json
{
  "data": {
    "input_user_id": "telegram_314062490"
  }
}
```

Example response:

```json
{
  "ok": true,
  "schema": "connection_hub.identity_family.v1",
  "linked": true,
  "platform_user_id": "02e...",
  "authority": {
    "kind": "authority",
    "authority_id": "platform",
    "provider": "platform",
    "user_id": "02e..."
  },
  "identities": [
    {
      "kind": "authority",
      "authority_id": "platform",
      "provider": "platform",
      "user_id": "02e..."
    },
    {
      "kind": "integration",
      "provider": "telegram",
      "provider_subject": "314062490",
      "user_id": "telegram_314062490",
      "integration_id": "telegram.kdcube_ref"
    }
  ],
  "memory_user_ids": ["02e...", "telegram_314062490"]
}
```

Consumers such as the memories app should use `memory_user_ids` server-side
when aggregating records across linked identities. The widget/client must not
provide arbitrary memory owner ids.

`delegated_identity_scope_resolve`:

```json
{
  "data": {
    "credential": {
      "schema": "kdcube.credential.v1",
      "credential_kind": "delegated_client_access",
      "issuer_authority_id": "delegated_client",
      "issuer_authenticator_id": "delegated_client.bearer",
      "subject": "integration:claude:02e...",
      "audience": "kdcube:delegated_client",
      "attrs": {
        "grantor_subject": "02e...",
        "client_id": "claude",
        "resource": "https://runtime/api/integrations/bundles/demo/demo/user-memories@2026-06-26/public/mcp/memories",
        "scopes": ["memories:read"],
        "tools": ["memory_search", "memory_get"],
        "identity_scope": "grantor_identity_family"
      }
    }
  }
}
```

Example response:

```json
{
  "ok": true,
  "schema": "connection_hub.delegated_identity_scope.v1",
  "delegate_identity": "integration:claude:02e...",
  "grantor_user_id": "02e...",
  "identity_scope": "grantor_identity_family",
  "memory_user_ids": ["02e...", "telegram_314062490"]
}
```

This is for already-verified delegated credentials. Product surfaces should use
it instead of parsing `grantor_subject` directly.

## Config keys that control these surfaces

Non-secret deploy props (see [../config/bundles.template.yaml](../config/bundles.template.yaml)):

- `connections.oauth.public_base_url` — public base for the shared connection
  callback (empty → derived from request host).
- `connections.delegated_to_kdcube.providers.<provider>.connector_apps` —
  connector apps, MANY per provider. This is the place where per-user external
  accounts become connectable; with no enabled connector app the provider is
  unconfigured.
- `identity.role_resolver.mode` — usually `platform`; `configured` is only for
  local demos until a platform principal/role resolver is wired.
- `identity.role_bindings` — optional local fixture used only when
  `identity.role_resolver.mode=configured`.
- `connections.delegated_to_kdcube.providers.icloud_mail.connector_apps` —
  configure app-password style mail connections such as iCloud.
- `ui.widgets.connections_settings.{enabled, src_folder, build_command}` — the
  widget build.
- `visibility.api.<alias>.*` / `visibility.widget.connections_settings.*` — access.
- `connections.delegated_credentials.admission.enabled` — expose direct
  protected-service admission and advertise it in protected-resource metadata.
- `connections.delegated_credentials.admission.services.<service_id>` — bind
  one authenticated service to catalog resource selectors through `secret_ref`.
  It does not redeclare operations or grants.
- `connections.delegated_credentials.admission.{max_clock_skew_seconds,nonce_ttl_seconds}`
  — signed-request freshness and replay retention.
- `connections.remote_mcp.{read_timeout_seconds,max_result_bytes}` — upstream
  MCP response bounds.
- `connections.remote_mcp.oauth.{enabled,client_name,public_base_url,state_ttl_seconds,expiry_leeway_seconds}`
  — owner browser authorization, callback origin, transaction lifetime, and
  proactive token refresh window for generic OAuth-protected MCP connectors.
- `connections.remote_mcp.outbound.{allow_http,allow_private_networks,allowed_hosts}`
  — deployment-owned endpoint policy. Public HTTPS is the default.

Deploy secret KEYS (see [../config/bundles.secrets.template.yaml](../config/bundles.secrets.template.yaml)):

- `connections.delegated_to_kdcube.oauth_state_secret` — signs the OAuth
  `state` for delegated-to-KDCube providers such as Gmail and Slack.
- `connections.delegated_to_kdcube.providers.<provider>.connector_apps.<connector_app_id>.client_secret`
  — per connector app (e.g.
  `connections.delegated_to_kdcube.providers.google.connector_apps.gmail.client_secret`,
  `connections.delegated_to_kdcube.providers.slack.connector_apps.demo.client_secret`).
- iCloud needs no deploy secret (the user's app-password is user-scoped state).
- `connections.delegated_credentials.admission.identity_projection_secret` —
  derives stable pairwise user and caller-profile ids for registered services.
- `connections.delegated_credentials.admission.services.<service_id>.signing_secret`
  — authenticates signed requests from one protected service.

No user credentials live in any descriptor. User OAuth tokens, app-passwords,
and external MCP upstream credentials are user-scoped secret state created
through these flows.

## Prerequisites

The hub requires external operator/user setup before a connection can work.
**Full step-by-step setup (Google Cloud Console, Slack app, iCloud) is in
[the integration guides](../../../../../docs/connection-hub/frontend/application/integrations/README.md)
(one article per provider).**
A summary follows:

### Slack (delegated-to-KDCube provider)

- A workspace admin creates an OAuth app in the Slack workspace.
- Configure the resulting OAuth client as a connector app:
  - Client ID → `connections.delegated_to_kdcube.providers.slack.connector_apps.<connector_app_id>.client_id`
  - Client Secret → secret `connections.delegated_to_kdcube.providers.slack.connector_apps.<connector_app_id>.client_secret`
- Add the redirect URI to the Slack app:
  `…/connection-hub@1-0/public/delegated_to_kdcube_oauth_callback`
- Request scopes: `search:read`.
- Set `connections.delegated_to_kdcube.oauth_state_secret` so OAuth `state` can be signed.

### Gmail (delegated-to-KDCube provider)

Gmail rides the **delegated-to-KDCube** framework (Google OAuth), same as Slack
— it gets per-connect claims, token refresh, and brokered credential
resolution for code acting on behalf of the current user.

- The Google OAuth client must have the delegated-to-KDCube callback added as
  an authorized redirect URI:
  `…/connection-hub@1-0/public/delegated_to_kdcube_oauth_callback`.
- Configure the client as a Google connector app:
  - Client ID → `connections.delegated_to_kdcube.providers.google.connector_apps.<connector_app_id>.client_id`
  - Client Secret → secret `connections.delegated_to_kdcube.providers.google.connector_apps.<connector_app_id>.client_secret`
- Scopes: `openid email profile gmail.readonly gmail.send` (send is needed for
  task email delivery). `connections.delegated_to_kdcube.oauth_state_secret`
  signs the state.

### iCloud Mail (delegated-to-KDCube — app-password)

- No admin/OAuth setup. The user creates an Apple **app-specific password** and
  enters it via `delegated_to_kdcube_connect_credential` through the
  `icloud_mail` provider and `app_password` connector app.

### Runtime

- The runtime must be refreshed so the new app loads, registers its
  named-service provider in discovery, and builds the `connections_settings`
  widget.

> Full step-by-step provider setup is in
> [the integration guides](../../../../../docs/connection-hub/frontend/application/integrations/README.md)
> (one article per provider).
