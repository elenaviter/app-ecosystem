---
id: connection-hub@1-0
title: "Connection Hub"
summary: "Connection Hub application: links external identities, brokers connected accounts, governs delegated cards and invocation policy, proxies user-owned external MCP tools, and evaluates live operation admission."
status: active
tags: ["app", "connection-hub", "identity", "connections", "named-services", "oauth", "email", "gmail", "icloud", "slack", "google-sheets"]
module: entrypoint
singleton: false
primary_surfaces:
  - "identity operations — link/resolve external identities to platform principal envelopes"
  - "request_authenticate public operation — verify provider/request proofs and return linked authority"
  - "named_service API (operations route) — serves the whole `connections` contract"
  - "public OAuth callback route (delegated_to_kdcube_oauth_callback) — shared by delegated to KDCube OAuth providers"
  - "delegated OAuth authorization server (public/oauth/*) — issues opaque delegated client credentials backed by live cards"
  - "remote_mcp_proxy public MCP surface — calls exact user-owned external tools selected on the caller card without disclosing the upstream credential"
  - "delegated_admission public operation — lets a registered external protected service evaluate one bearer/resource/operation against current authority"
  - "connections_settings widget (ui/widgets/connections)"
links:
  config: config/bundles.template.yaml
  secrets: config/bundles.secrets.template.yaml
  interface: interface/README.md
  openapi: interface/connection-hub.openapi.yaml
  design: ../../../../docs/connection-hub/frontend/application/README.md
  architecture: ../../../../docs/connection-hub/connection-hub-architecture.md
  journal: ../../../../journal/README.md
updated_at: 2026-09-02
---

# Connection Hub App

Connection Hub is the product; `connection-hub@1-0` is its current technical
KDCube app id. Its portable contracts come from the `connection-hub` Python
distribution. It is the user-scoped hub for connection edges, connected
accounts, delegated cards, and current operation admission.

It answers eight related questions:

```text
Who is this external identity in KDCube?
  -> connection edge -> platform user id -> platform principal/role resolver

Can this incoming request prove one of those external identities?
  -> request authenticator -> connection edge -> UserSession authority

Which user ids belong to the same linked identity family?
  -> Connection Hub resolver -> platform authority + provider identities

Can automation use this user's external account?
  -> connected account -> delegated token/claim

What may this external app, agent, or automation do for the approving user?
  -> delegated bearer -> current card -> active capability catalog

May this granted operation run repeatedly or one time?
  -> exact card/resource/operation -> current invocation policy

Can this caller use a remote MCP service whose owner did not integrate it with Connection Hub?
  -> user-owned connector -> exact selected tool -> Connection Hub proxy

May this registered external backend perform this concrete delegated operation?
  -> service proof + delegated bearer -> live admission decision
```

The app exposes request authenticators so ingress and app/channel handlers can
verify Telegram/webhook/API-key style requests without duplicating proof logic.
It also exposes the public `connections` named-service provider so any app acting
for the current user can resolve that user's connection tokens without owning the
OAuth mechanics itself. It also exposes connection-edge operations so a verified
external identity can resolve to a platform principal envelope, and a resolver
operation so aggregation surfaces can get canonical linked user ids server-side.

The app wires these building blocks:

- connection-edge storage and a temporary principal-resolution fixture;
- identity-family resolver for linked user-id expansion;
- request authenticators, currently Telegram Mini App/WebApp `initData`;
- the reusable `integrations/connections` mechanics (`ConnectionStore`,
  `ConnectionsProviderBase`, the connection registry of `ConnectionProvider`s);
- the reusable `integrations/email` settings (**iCloud** app-password only —
  Gmail is a connections provider), exposed through its own `email_*` ops; and
- owner-scoped external MCP connectors, descriptor-drift review, delegated
  proxy dispatch, and separate once-or-always invocation policy; and
- a `connections_settings` browser widget served from `ui/widgets/connections`.

The canonical distinction between edges, connected accounts, cards, catalogs,
service registrations, and their stores is in the
[Connection Hub architecture](../../../../docs/connection-hub/connection-hub-architecture.md).

## Identity model

```text
external proof from a channel/provider
  provider="google"   subject="person@example.com"
  provider="telegram" subject="314062490"
  provider="bundle"   subject="some-app:external-user-77"
        |
        v
Connection Hub connection edge
        |
        v
platform principal/role resolver
        |
        v
platform_user_id + roles/permissions
```

Connection Hub should not be the long-term role authority. Its local
`identity.role_bindings` config is only a development fixture. Real entitlements
must come from a platform principal/role resolver after identity resolution.

## Request-authenticator model

```text
gateway/app request
  -> request_authenticate(RequestEnvelope)
  -> configured authenticator verifies provider proof
  -> connection edge resolves provider:<subject>
  -> platform authority is projected into identity_authority
  -> gateway turns result into UserSession
```

Telegram is the first provider-family implementation. Multiple bots can be
configured as descriptor rows under `identity.authenticators[]` or as
widget-managed Postgres metadata rows. Each row references a secret key in
`bundles.secrets.yaml`; secret values are never stored in the metadata row.
Each app/provider surface has stable non-secret authority/authenticator ids.
Controlled app surfaces, such as the Workspace Telegram Mini App, read those ids
from app config and send `X-KDCube-Auth-Authority-ID` and
`X-KDCube-Auth-Authenticator-ID` beside the provider proof. If a request names
an authenticator id, Connection Hub tries only that row and fails closed if it
is not configured.

`role_providing` marks authenticators that directly prove platform authority.
Telegram bots normally keep it `false`: Telegram proves the actor, then the
connection edge supplies platform roles.

### Proof-based Telegram linking

First-time Telegram linking needs two proofs in one flow: a platform-authenticated
browser session and a Telegram-signed Mini App session.

```text
Platform-first:
  KDCube browser session
  -> connections_settings creates short-lived challenge for platform_user_id
  -> user opens the provider proof surface that owns the desired Telegram bot
  -> that Telegram Mini App sends signed initData to Connection Hub
  -> Connection Hub validates initData and completes:
       telegram:<telegram_user_id> -> platform_user_id

Telegram-first:
  user opens a Telegram Mini App from Telegram
  -> host app embeds the Connection Hub widget in an iframe
  -> host app passes opaque authContext.headers through CONFIG_RESPONSE
  -> Connection Hub widget sends signed initData to Connection Hub
  -> Connection Hub creates pending provider proof with no platform_user_id
  -> Connection Hub widget claims a short-lived connection-hub Socket.IO session
  -> user opens returned platform_claim_url
  -> standalone claim page uses /api/cp-frontend-config to sign into KDCube
  -> connections_settings claims the proof for the current platform user
  -> Connection Hub emits connection_hub.edge.changed to the iframe
  -> Connection Hub completes:
       telegram:<telegram_user_id> -> platform_user_id
```

The Telegram request never sends or chooses `platform_user_id`; it only proves
the Telegram account. The platform user is either server-side on the
platform-first challenge or supplied by the authenticated KDCube claim request
in the Telegram-first flow.

The Telegram-first flow is evented, not polled. The embedded Connection Hub
widget creates its own app-scoped live channel with `federated_data_bus_claim`.
`telegram_connection_edge_start` stores that live session id on the pending
challenge. When the browser-side claim completes, Connection Hub emits a
targeted `connection_hub.edge.changed` service event to that session,
and the iframe refreshes its linked/unlinked state.

## Three-level connection model

```text
provider            connector app                    user account
(OAuth mechanics)   (credentials, MANY per provider) (one user token, records connector_app_id)
  slack       ->      connector_app_id=acme-search  --->  account_id (workspace) + token
  slack       ->      connector_app_id=other-app    ---->  ...
```

- **Provider** = OAuth mechanics only, no credentials. Providers are DYNAMIC —
  driven by the connection registry (any registered `ConnectionProvider`).
  Importing the providers package registers the built-ins (Slack, …).
- **Connector app** = the OAuth application client or credential class that carries credentials. Deploy
  config populates these, MANY per provider, under
  `connections.delegated_to_kdcube.providers.<provider>.connector_apps`.
  Each connector app's `client_secret` is supplied separately as a deploy secret
  when the provider uses OAuth.
- **User account** = connected THROUGH one connector app; the account record stores
  its `connector_app_id`. Tokens are user-scoped, so any
  bundle acting for that user can resolve them.

The OAuth callback is a single hub-level route shared by all providers/apps:
`…/connection-hub@1-0/public/delegated_to_kdcube_oauth_callback`, signed by
`connections.delegated_to_kdcube.oauth_state_secret`.

## Email integration

**Gmail, Slack, Google Sheets, and Google Docs ride delegated to KDCube**
(OAuth) through
`delegated_to_kdcube_start_oauth` + the shared
`delegated_to_kdcube_oauth_callback`. **iCloud** uses the same delegated-to-KDCube
broker through `delegated_to_kdcube_connect_credential`, because
it is app-password based rather than OAuth based.

A server-side caller resolves the user's external credential through the
delegated to KDCube broker. It returns an unavailable result if that user has
not connected the requested provider/account.

## Layout

```text
connection-hub@1-0/
  AGENTS.md
  entrypoint.py             # thin KDCube host composition over Connection Hub
  config/
    bundles.template.yaml
    bundles.secrets.template.yaml
  interface/
    README.md
    connection-hub.openapi.yaml
  surfaces/
    delegated_admission.py # direct protected-service host adapter
    remote_mcp.py           # request-scoped delegated MCP proxy
  tests/
    test_delegated_access_create.py
    test_oauth_discovery.py
  ui/
    widgets/
      connections/          # connections_settings widget app
```

Portable authority logic lives in the Connection Hub package. KDCube-specific
transport, identity-provider, and named-service bindings remain host adapters:

```text
connection-hub
  hub/                       # edges, identity families, authenticator metadata
  delegated_credentials/    # cards, catalog, OAuth, admission policy
  invocation_policy/        # once/always and invocation idempotency
  remote_mcp/               # user-owned connectors, drift, proxy contracts
  delegated_to_kdcube/      # connected-account lifecycle and provider contracts

kdcube_ai_app.apps.chat.sdk.integrations.connection_hub
  hub/provider_impl.py       # KDCube named-service and account-store binding
  hub/authenticators.py      # KDCube provider-proof and session projection
  delegated_credentials/oauth/http/  # FastAPI transport adapter
```

## Runtime notes

- On `on_bundle_load` the app registers its named-service providers into Redis
  discovery (`bundle_registry` transport) for this tenant/project, so other
  apps can discover the `connections` provider.
- Connection tokens are user-scoped state in app storage; they are never put in
  descriptor templates.
- Identity links are also user-scoped state in app storage. They link a
  verified external identity to a platform user; they do not grant roles by
  themselves.
- Direct protected-service admission is disabled by default. When enabled, a
  registered service supplies an independent signed workload proof beside the
  delegated bearer. The route returns pairwise user and caller-profile ids and
  current operation authority, never raw Connection Hub ids or a provider
  credential.
- External MCP connectors store upstream credentials in the owner's server-side
  secret store. Durable connector revisions and browser responses contain only
  a credential-presence signal and non-secret descriptor metadata.
- The external MCP proxy resolves card, connector, exact tool descriptor, and
  invocation policy on every call. A changed or removed selected tool is denied
  before dispatch.
- [Protect an external backend with Connection
  Hub](../../../../docs/connection-hub/recipes/direct-protected-service.md) provides the
  deployment contract and links a runnable protected-service implementation.
- The static widget is built from `ui/widgets/connections`; the runtime must be
  refreshed so the new app loads and the widget is built.

See [AGENTS.md](AGENTS.md) for builder-agent onboarding,
[interface/README.md](interface/README.md) for the contract,
[config/bundles.template.yaml](config/bundles.template.yaml) for non-secret
deploy props, [config/bundles.secrets.template.yaml](config/bundles.secrets.template.yaml)
for deploy secret keys,
[the frontend documentation](../../../../docs/connection-hub/frontend/application/README.md)
for the UI design, the
[Connection Hub architecture](../../../../docs/connection-hub/connection-hub-architecture.md)
for semantic and storage ownership, and [the public journal index](../../../../journal/README.md)
for the centralized maintenance record.
