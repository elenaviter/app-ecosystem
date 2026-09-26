---
id: connection-hub/frontend/application
title: "Connection Hub Design"
summary: "KDCube-hosted application composition for connection-hub@1-0: connection edges, connected accounts, delegated credentials, direct admission, OAuth callbacks, named-service boundaries, and the Connections widget."
status: active
tags: ["app", "connection-hub", "identity", "connections", "named-services", "mcp", "oauth", "delegated-credentials", "email", "design"]
keywords: ["connection hub app", "delegated access", "connected accounts", "client metadata", "grant mutation csrf", "live grant authority"]
updated_at: 2026-09-11
see_also:
  - ../../connection-hub-architecture.md
  - ../../package/extraction-architecture.md
  - ../../package/delegated-cards.md
  - ../../package/oauth-delegated-credential-protocol.md
  - https://github.com/kdcube/kdcube/blob/main/app/ai-app/docs/sdk/bundle/bundle-platform-integration-README.md
---

# Connection Hub Design

`connection-hub@1-0` is the KDCube-hosted Connection Hub app for connecting
external identities and provider accounts to KDCube, and for delegating bounded
KDCube access back out to external clients.

Connection Hub owns the portable storage contracts, rendering source, edit,
enforcement, revocation, and descriptor-drift semantics for the **Delegated by
KDCube** cards this app renders. KDCube supplies the concrete storage and
runtime adapters. This page owns the KDCube-hosted application composition;
the [Connection Hub architecture](../../connection-hub-architecture.md) owns
the cross-surface semantic and storage map.

It has three responsibilities that must stay separate:

```text
connection edges
  external proof -> platform user id
  examples: google:person@example.com, telegram:314062490, bundle:app:user-77
  used by: auth bridges, inbound hooks, cross-channel user routing

delegated account connections
  platform user id -> external account credential with approved provider claims
  examples: Gmail OAuth token, Slack workspace OAuth token, iCloud app password
  used by: automation that acts for the user

delegated client credentials
  platform user -> external automation allowed into selected KDCube boundaries
  examples: MCP OAuth connector, manual script/agent bearer
  used by: external clients acting for the approving KDCube user

protected-service admission
  registered external backend + opaque delegated bearer -> live decision
  used by: a backend that enforces its own operation without a KDCube door
```

Do not infer platform roles from delegated accounts. The long-term authority is:

```text
verified external identity
  -> Connection Hub connection edge
  -> platform principal/role resolver
  -> platform user id + roles/permissions
```

## What it is

A user-scoped hub that:

- stores external connection edges for the current platform user;
- creates short-lived connection-edge challenges for proof flows such as
  Telegram Mini App linking;
- resolves a verified external identity to a platform principal envelope;
- resolves a current actor/platform user to its linked identity family and
  canonical user ids for aggregation surfaces such as memories;
- serves the public `connections` named-service contract over HTTP via the
  `named_service` op, and registers that provider into discovery so other apps
  can resolve delegated tokens (`bundle_registry` transport);
- owns the single shared OAuth callback route used by all providers/apps;
- offers a Connections settings widget for users to link identities and
  connect/disconnect accounts;
- issues and revokes external-client credentials through OAuth consent or
  **Delegated by KDCube -> Create automation access**;
- presents bounded client-reported metadata on connected-app Cards, and lets
  the owner search all metadata text or filter by an exact key and value;
- renders exact named-service namespace operations for manual automation access,
  validates them against the descriptor, and persists the narrowed policy in
  the delegated grant record.
- authenticates a registered external protected service and evaluates its
  bearer/resource/operation request against the same current card and active
  catalog used by KDCube-managed REST/MCP guards.

Client metadata is folded by default because registration documents may carry
many fields. The filter's metadata-key menu is the sorted union of keys on the
Cards currently returned to the owner; its value input uses case-insensitive
substring matching. The panel labels this data as client-reported. It never
uses it to decide authority, and it does not manufacture a machine, session,
connector alias, or worker identity that the OAuth client did not report.

## Building blocks it wires

- **SDK `connections.hub.connection_edges`** — connection-edge storage and the temporary
  principal-resolution fixture, plus one-time connection-edge challenges. This
  module deliberately returns a
  `role_resolution` envelope that points to the future platform principal/role
  resolver instead of treating the app as the role authority.
- **SDK `connections.hub.resolver`** — identity-family resolver. Given the current
  actor/platform user id, it returns platform authority identity,
  provider/integration identities, and `memory_user_ids` for server-side
  aggregation.
- **`integrations/connections`** — `ConnectionStore` (user-scoped, shared-tokens),
  `ConnectionsProviderBase`, and the connection registry of `ConnectionProvider`s.
  Importing the providers package registers the built-ins (Slack, …).
- **SDK `connections.hub.provider_impl`** — `ConnectionHubProvider`, the concrete
  `connections` provider. It makes the STORAGE CHOICE explicit (user-scoped
  `ConnectionStore`) and resolves the per-request user from the named-service
  context. `get_token` auto-picks only when exactly one account is connected,
  otherwise raises `AmbiguousConnectionAccount`.
- **SDK `connections.hub.authenticators`** — authenticator modules for request
  authentication. Telegram proof verification is implemented. Slack, OIDC,
  Google, webhook HMAC, and API-key providers are explicit module slots, not
  role authority shortcuts.
- **SDK `connections.hub.authenticator_store`** — Postgres store for
  widget-managed request-authenticator metadata. It stores `secret_ref`, never
  secret values. See [storage/README.md](storage/README.md).
- **`integrations/email`** — iCloud app-password settings, exposed through
  dedicated `email_*` ops. Gmail is not handled here; it is a `connections`
  provider.
- **`connections_settings` widget** — built from `ui/widgets/connections`.
- **Platform sign-in administration** — the Authenticators tab validates and
  writes only the enabled staged descriptor sections. Provider edits publish
  KDCube's value-free platform-settings notification; the response says
  whether active ingress subscribers received it or a runtime refresh is
  required. The sign-in lane itself remains refresh-only.

> Current correction: Gmail rides the `connections` framework. The email
> integration serves iCloud app-password settings only.

## The three-level model

```text
provider (mechanics, no creds)
  -> connector app (operator app config; MANY per provider; carries client_id/secret + claim ceiling)
     -> user account (one credential, records connector_app_id and approved claims)
```

Providers are dynamic (registry-driven). Connector apps are deploy config under
`connections.delegated_to_kdcube.providers.<provider>.connector_apps`.
Accounts are user state created by the OAuth or credential flow. The delegated
integration OAuth callback is shared by the OAuth providers and signed by
`connections.delegated_to_kdcube.oauth_state_secret`.

Application tool claim policy is not stored as a separate capability registry.
A tool declares its own provider/connector/claims requirements beside the tool
definition, for example under `tool_claims.<tool>.connections.delegated_to_kdcube.connected_accounts`.
The SDK resolves those declarations into `ToolClaimPolicy` objects.

The first runtime integration is agent preflight: when a ReAct agent starts a
turn, it checks all configured tool claim policies for that agent against the
current platform user's connected accounts. If something is missing, the
workflow emits a structured `needs_connected_account_consent` chat step with a
Connection Hub URL. The chat UI shows a banner, the user opens Connection Hub
to connect/upgrade the account, and then retries the request. Later tool-level
runtime checks can reuse the same payload shape.

Connection Hub may scan tool declarations for admin visibility, but the
application bundle/tool definition remains the source of truth for which
provider claims a tool needs.

## Delegated automation and provider-backed named services

The generic named-services MCP resource has two authorization layers and may
have a third provider prerequisite:

```text
managed MCP boundary
  resource + generic MCP tool + named_services:use
        |
        v
named-service boundary
  namespace + selected operation + namespace grants
        |
        v
connected provider boundary (only when declared)
  grantor's account + provider claims
```

The manual **Create automation access** screen projects the first two layers
from `connections.delegated_credentials.oauth.resources[]`. For a resource with
`named_services`, it submits:

```text
named_service_operations
  <resource>
    <namespace>
      [<existing descriptor operation>, ...]
```

`AutomationAccessService` validates the exact selection and stores a narrowed
copy of the descriptor's `named_services` policy in `GrantStore`. KDCube
Services uses that stored policy for its runtime `NamedServiceBoundaryCatalog`,
so unselected namespaces and operations are denied without a second registry.
Pointer-backed OAuth and `kst1` credentials resolve this record on
every managed call and refresh. Missing, expired, malformed, unavailable, or
mismatched live authority fails closed; a pointed credential does not recover
its older embedded grant snapshot.

A provider may separately publish `metadata.connected_accounts`. Connection
Hub renders those requirements and links to **Delegated to KDCube**, preserving
flat or operation-specific claim structure. This is presentation and consent
guidance only: provider discovery metadata and provider credentials are never
copied into the automation grant. The provider resolves the grantor's connected
account at call time.

## Surfaces

- `named_service` (operations) — the whole `connections` contract.
- `connection_edges_list`, `connection_edge_upsert`, `connection_edge_remove`,
  `connection_edge_challenge_create`, `connection_edge_challenge_status`,
  `identity_resolve`, `identity_family_resolve`,
  `delegated_identity_scope_resolve` (operations) — connection-edge management,
  proof challenges, principal resolution, linked-family user-id expansion, and
  delegated credential identity-scope resolution.
- `telegram_connection_edge_complete` (public) — validates Telegram Mini App
  `initData` and completes a pending Telegram link challenge.
- `telegram_connection_edge_status`, `telegram_connection_edge_start`,
  `telegram_connection_edge_remove` (public) — Telegram-first Mini App linking:
  read current link, create a provider-proof challenge, and unlink the current
  Telegram subject.
- `federated_data_bus_claim` (public) — validates the promoted request auth
  context and returns a short-lived Socket.IO token for the Connection Hub
  widget's own live channel.
- `request_authenticate` (public) — platform/app request-auth selector endpoint:
  request envelope in, verified provider identity plus linked platform authority
  out.
- `authenticators_list`, `authenticators_upsert`, `authenticators_remove`
  (operations) — admin/widget APIs for request-authenticator metadata. These
  APIs reject secret values; use `secret_ref` and bundle secrets. Controlled
  surfaces carry `X-KDCube-Auth-Authority-ID` and
  `X-KDCube-Auth-Authenticator-ID`.
- `delegated_to_kdcube_catalog`, `delegated_to_kdcube_start_oauth`,
  `delegated_to_kdcube_connect_credential`, `delegated_to_kdcube_disconnect`,
  `delegated_to_kdcube_resolve` (operations) — current widget and server-side
  broker helpers for delegated external accounts.
- `delegated_to_kdcube_oauth_callback` (public) — the shared OAuth browser
  redirect for delegated to KDCube providers such as Gmail and Slack.
- `delegated_admission` (public, disabled by default) — direct operation-level
  admission for a registered external backend. It requires an opaque delegated
  bearer plus a separate signed service proof and returns a service-scoped
  principal without provider credentials.
- `delegated_access_list`, `delegated_access_create`,
  `delegated_agent_grant_create`, `delegated_access_revoke` (operations) —
  list, issue, extend, and revoke delegated access. State-changing Connection
  Hub operations opt into proc's one-time CSRF contract for
  cookie-authenticated browsers, including grant, connected-account,
  connection-edge, authenticator, DCR-allowlist, email-account, and generic
  named-service mutations. Every effective POST operation is classified in
  `CSRF_PROTECTED_OPERATION_ALIASES` or
  `CSRF_EXEMPT_POST_OPERATION_ALIASES`; public protocol POSTs have a separate
  explicit exemption inventory. Explicit token and authenticated internal
  calls keep their existing request proof. Manual named-service access accepts the exact
  `named_service_operations[resource][namespace][]` selector.
- `project_agent_card_get`, `project_agent_card_update`,
  `project_agent_card_apply_profile` (operations, W319) — open and change an
  agent's Card as someone other than its owner. The project host
  (`project_agent_card_authorize`) answers for the owner, a project admin, and
  a platform admin (read only); an owner's share is decided here. Every
  change is written under the owner's storage key with the acting person in a
  `project_agent_card_audit` provenance entry. A deployment without a project
  host answers 503 `project_agent_card_authorization_unavailable` (reason
  `project_agent_card_provider_not_configured`), not a denial.
- `project_control_card_get`, `project_control_card_update` (operations,
  W260) — read and change a project's Control Card as someone other than the
  person who created it (the creator's own `control_card_get` and
  `control_card_update` find only the creator's Cards). The project host
  (`project_control_card_authorize`) answers under the person's session with
  `via` `owner`, `project_admin` (a person whose project Card holds
  `project.control.update`) or `project_member` (read only), and names the
  Card's creator. Every change is written under the creator's storage key
  with the acting person in a `project_control_card_audit` provenance entry.
  - **A save needs every permission the Card carries.** The acting person
    must be able to delegate each grant on the Card after the save, not only
    the ones they add, so an admin who holds fewer permissions than the Card
    carries can save no change at all. The refusal
    (`delegated_access_grants_not_delegable`) names the grants and says so.
  - **The save runs without the acting person's platform roles.** A Card
    offering a resource that only a role may choose (an admin-only resource)
    cannot be saved on this path; the creator's own path is unaffected.
  - A deployment without a project host answers 503
    `project_control_card_authorization_unavailable` (reason
    `project_control_card_provider_not_configured`), not a denial.
- **Who edits which Card in a project.** Cards held by a Problem Board project
  (the project Control Card, each person's Control Card, each agent's project
  Card) are changed by that Problem Board project's admins (the project's own
  admin role, not a KDCube user role), reached through the project's Team >
  People; a member changes only their own My Card, within their Control Card.
  Every other Control Card keeps its normal editing here. The full Problem Board
  page comes with the public Problem Board documentation (W340). The mechanics
  on this side:
  - `project_person_control_get` (W260) answers `viewer: {can_edit: false,
    edit_in_project, reason}`: a person's Control Card is read only in this
    view for everyone. A project admin gets a link to the project's Team >
    People, where it is edited; a member reads their own
    (`project_person_control_decided_by_admin` for their own update or
    revoke); an unanswerable policy says so. A project admin writes any
    person's Control Card, their own included, through the project path.
  - `project_control_card_attach`, `project_control_card_detach` (operations,
    W260) attach or detach a project's Control Card on an agent's Card that
    another person owns. Two host answers are needed: the project host's
    `project_control_card_authorize` with action `attach` (it names the
    Card's creator) and W319's agent answer (it names the owner; a project
    admin linking an agent that does not attend yet is accepted here and for
    no other agent-Card change).
    The Control Card side is the project host's `attach` answer: the owner or
    a project admin, or (W260) the agent's owner while linking their own agent
    (`owner_linking`, accepted by attach only; attaching only narrows) or
    while unlinking it (`owner_unlinking`, accepted by detach only, as part of
    leaving the project). Any other pairing is refused
    (`decision_via_cannot_bind`).
    The attach and detach questions name the agent Card (`access_id`) beside
    `control_id`, `project_ref` and `action`, so the host can check that the
    asker owns that agent; read and write questions do not.
  - The binding then records `holder_subject`, the Control Card's creator, and
    admission resolves the Control Card under it. Only the project path writes
    it, a foreign holder requires `and` composition (the Control Card only
    narrows), and the agent's owner cannot detach or replace it on the plain
    path (`control_card_held_by_project`). Bindings without a holder keep the
    rule that the Control Card is the Card grantor's own.
- `agent_card_share`, `agent_card_unshare`, `agent_card_shares` (operations,
  W319) — the owner shares an agent's Card with a named person at `view`
  (open read-only) or `edit` (also change it, and apply a profile such as
  coordinator), stops sharing, and lists the shares. The share is stored next
  to the Card (`cards/<access_id>/shares/`); an unshare leaves a `revoked`
  record, so it takes effect at once and the person is told why.
- `agent_card_shared_with_me` (operation, CSRF-exempt read, W319) — the
  agents shared with the signed-in person, and the shares revoked since;
  the project host reads it under the person's session for their pool. A
  widget link with `shared=1` opens a shared Card without a project.
- `email_accounts_status`, `email_connect_app_password`, `email_disconnect_account`
  (operations) — older iCloud-only email integration surface.
- `connections_settings` (widget) — the React/Redux settings UI.
- `site_config` (public) — what the standalone site shell needs before any
  user is signed in: the application id, site alias, widget alias, tenant,
  project, and the platform endpoints it bootstraps from. Nothing
  user-specific.

## Standalone site

Connection Hub also ships as its own site. The application declares a main
view (`ui/main`: `index.html`, `site.js`, `site-routing.js`, `styles.css`,
copied as-is at build)
and registers it as an application site:

```yaml
ui:
  main_view:
    src_folder: ui/main
    build_command: cp index.html site.js site-routing.js styles.css <VI_BUILD_DEST_ABSOLUTE_PATH>/
    site:
      enabled: true
      alias: connections     # served at /sites/connections/
      default: false         # true would also make it the runtime's root site
      hosts: []              # public hostnames that resolve to this site at /
      title: Connection Hub
```

The platform serves the shell at `/sites/<alias>/` (and at the root of every
host in `hosts`), sets `<base href>` accordingly, and injects a
`#kdcube-site-context` script carrying tenant, project, and application id.
The platform requires `proxy.route_prefix` to be non-root while any site is
enabled.

What the shell owns, and what it does not:

- It owns the page chrome (brand, tenant/project scope, identity, sign in and
  sign out, a link back to the platform) and the signed-out state. It
  bootstraps from `public/site_config`, then `/api/cp-frontend-config` and
  `/profile`, and re-probes on `kdcube-auth-changed`.
- It hosts the `connections_settings` widget in an iframe served from the
  widget's own bundle route, so the widget resolves tenant, project, and
  application from its URL exactly as in every other host. The shell passes
  the widget's allowlisted direct-link fields into that iframe. A `?tab=` (or
  `#tab`) selects the tab, and an exact delegated Card selector such as
  `access_id`, `manual_access_id`, or `control_card_id` opens that Card. For
  example, `/sites/connections/?tab=delegatedAccess&access_id=<id>` opens the
  named Card. Unknown fields are not copied into the iframe URL.
- A `control_card_id` link also names how the Card is reached, and the widget
  reads and saves it on that path:
  - `control_card_id` alone: the creator's own Control Card
    (`control_card_get`, `control_card_update`).
  - `control_card_id` + `project_ref`: a project's Control Card, through the
    project (`project_control_card_get`, `project_control_card_update`), so
    any project admin can open and change it, not only its creator; a project
    member reads it.
  - `control_card_id` + `project_ref` + `target_subject` (or `invitation_ref`):
    a person's Control Card in the project (`project_person_control_get`).
- Because the widget route is authenticated and an iframe request is not a
  top-level navigation, the shell mounts the widget only after `/profile`
  confirms a session. Signed out, it sends the visitor to the platform
  sign-in page with `next` set to the site URL (`/signin/` by default, the
  page the platform's own widget bounce uses; `site.sign_in_url` overrides
  it) - automatically once per page load, so an expired session cookie heals
  without a click, and again on the Sign in button. The session itself is
  owned by the platform frontend; the shell only reads `/profile`.
- It answers the widget's `CONFIG_REQUEST` with the runtime configuration and
  relays `kdcube-auth-required` into the login flow.

Directly opening the widget as a page still works without the site:
`/api/integrations/bundles/<tenant>/<project>/connection-hub@1-0/widgets/connections_settings?tab=<tab>`
bounces a signed-out top-level navigation through the platform sign-in.

## Telegram Mini App embedding

Workspace hosts the Connection Hub widget in its Telegram Mini App Connect tab.
This uses the same widget handshake as scene-hosted widgets:

```text
Connection Hub iframe
  -> CONFIG_REQUEST(identity=CONNECTIONS_WIDGET)
Workspace Telegram host
  -> CONFIG_RESPONSE(config.authContext.headers)
Connection Hub iframe
  -> public Connection Hub APIs with promoted authContext.headers
```

The child iframe does not read `window.parent.Telegram`, does not know bot
tokens, and does not call Workspace APIs for Connection Hub work. It receives
an opaque header map and promotes it onto its own requests. Connection Hub then
validates the request through its authenticator modules.

For link completion, the child iframe creates a Connection Hub live channel:

```text
iframe -> federated_data_bus_claim
       -> short-lived Data Bus token backed by an actor UserSession
       -> Socket.IO session scoped to connection-hub@1-0
       -> telegram_connection_edge_start(live_event_session_id)
browser claim -> connection_edge_challenge_status
       -> explicit user confirmation
       -> connection_edge_challenge_claim(confirmed=true)
       -> connection_hub.edge.changed to the iframe session
       -> iframe reclaims/reconnects and refreshes link status
```

There is no polling in this flow. The browser-side claim returns normally for
the browser user, and Connection Hub separately signals the original Telegram
Mini App iframe that initiated the provider-proof challenge.

The Data Bus claim is owned by Connection Hub, not the host app. For unlinked
Telegram users it creates a low-authority actor session such as
`telegram_100200300`. After a link exists, the next claim keeps that actor id
and projects the linked platform authority into `session.identity_authority`.

The standalone browser claim page performs its own KDCube platform sign-in using
`/api/cp-frontend-config`; it does not depend on website auth code. The dated
implementation history is cataloged in the
[public journal pointer](../../../../journal/README.md).

See [the app interface](../../../../products/connection-hub/apps/connection-hub@1-0/interface/README.md) for the full contract and
prerequisites, [storage/README.md](storage/README.md) for storage boundaries,
and the [public journal pointer](../../../../journal/README.md) for build decisions.
