---
id: connection-hub@1-0/agents
title: "Connection Hub Builder-Agent Onboarding"
summary: "Builder-agent onboarding guide for the platform Connection Hub example app: connection edges, connected provider accounts, delegated client credentials, OAuth callbacks, named-service boundaries, and the Connections widget."
status: "active"
tags: ["agents", "builder", "onboarding", "connection-hub", "identity", "connections", "oauth", "mcp", "named-services", "delegated-credentials", "react", "redux"]
see_also:
  - "./README.md"
  - "../../../../docs/connection-hub/connection-hub-architecture.md"
  - "../../../../docs/connection-hub/frontend/application/README.md"
  - "../../../../docs/connection-hub/frontend/application/storage/README.md"
  - "./interface/README.md"
  - "./interface/connection-hub.openapi.yaml"
  - "./config/bundles.template.yaml"
  - "https://github.com/kdcube/kdcube/blob/main/app/ai-app/docs/service/auth/app-hosted-platform-login-and-session-README.md"
  - "https://github.com/kdcube/kdcube/blob/main/app/ai-app/docs/service/auth/app-simple-idp-bridge-README.md"
  - "https://github.com/kdcube/kdcube/blob/main/app/ai-app/docs/sdk/solutions/ecosystem-component/components-ecosystem-README.md"
  - "https://github.com/kdcube/kdcube/blob/main/app/ai-app/docs/sdk/namespace-services/README.md"
---

# Connection Hub Builder-Agent Onboarding

This is the builder-agent landing page for `connection-hub@1-0`.

The app is the playground for connecting external identities and delegated
accounts into KDCube. Keep these two concepts separate:

```text
connection edge
  purpose: prove that an external identity belongs to a platform user
  examples: google:elena@example.com, telegram:314062490, bundle:app:user-77
  used by: auth bridges, inbound webhooks, channel-specific entrypoints

connected account
  purpose: let automation use a user's external account with delegated access
  examples: Gmail OAuth token, Slack workspace token, iCloud app password
  used by: user automation and app workflows that act for the user

delegated client credential
  purpose: let an external script, agent, or MCP client enter selected KDCube
           resources and operations on behalf of an approving platform user
  examples: manual automation bearer, OAuth-issued MCP connector credential
  used by: managed REST/MCP guards and the named-service bridge

protected-service registration
  purpose: authenticate an external policy-enforcement backend and bind it to
           the catalog resources for which it may request live decisions
  examples: crm-api may ask about https://api.example.test/customers*
  used by: direct delegated_admission calls; never duplicates operations/grants

request authenticator
  purpose: verify that an incoming request proves a channel identity, then
           project linked platform authority into a UserSession
  examples: Telegram initData, Slack signature, webhook HMAC, API key
  used by: gateway auth selector and app/channel handlers
```

Do not infer platform roles from a delegated account token. A Gmail token can let
automation read/send mail, but it does not prove admin rights. The target flow is:

```text
verified external identity
  -> Connection Hub connection edge
  -> platform principal/role resolver
  -> platform user id + roles/permissions
```

`connection-hub@1-0` may include a configured role-binding fixture for local
demos, but that fixture is not the long-term security authority.

## Read First

Start with these app-local files:

- [README.md](README.md)
- [Connection Hub architecture](../../../../docs/connection-hub/connection-hub-architecture.md)
- [frontend design](../../../../docs/connection-hub/frontend/application/README.md)
- [storage map](../../../../docs/connection-hub/frontend/application/storage/README.md)
- [public journal index](../../../../journal/README.md)
- [interface/README.md](interface/README.md)
- [interface/connection-hub.openapi.yaml](interface/connection-hub.openapi.yaml)
- [config/bundles.template.yaml](config/bundles.template.yaml)
- [config/bundles.secrets.template.yaml](config/bundles.secrets.template.yaml)
- [entrypoint.py](entrypoint.py)
- Connection Hub authority core: [`connection-hub`](../../packages/connection-hub/src/connection_hub)
- KDCube host adapters:
  [`kdcube_ai_app.apps.chat.sdk.integrations.connection_hub`](https://github.com/kdcube/kdcube/tree/main/app/ai-app/src/kdcube-ai-app/kdcube_ai_app/apps/chat/sdk/integrations/connection_hub)
- [ui/widgets/connections/src/App.tsx](ui/widgets/connections/src/App.tsx)

When changing auth/session behavior, also read the platform docs:

- [Application-hosted platform login and session](https://github.com/kdcube/kdcube/blob/main/app/ai-app/docs/service/auth/app-hosted-platform-login-and-session-README.md)
- [SimpleIDP application bridge](https://github.com/kdcube/kdcube/blob/main/app/ai-app/docs/service/auth/app-simple-idp-bridge-README.md)

Read the centralized journal before changing behavior. Add a dated journal entry for every
implementation round that changes API contracts, auth semantics, storage shape,
widget behavior, or deployment config.

## Product Shape

```text
Connection Hub app
  entrypoint.py
    operations API
      - connections_*: delegated account connection helpers
      - identity_*: connection edge and principal-resolution helpers
      - request_authenticate: provider-proof verification for request auth
      - email_*: iCloud app-password helper ops
    public OAuth callback
      - connection_oauth_callback

  named service provider
    namespace: connections
    purpose: cross-app token resolution for delegated accounts

  widget: connections_settings
    source: ui/widgets/connections
    stack: React + Redux Toolkit
    purpose: one user-facing surface for connection edges and connected accounts

  delegated access
    OAuth consent or manual Create automation access
    resource_grants + selected top-level operations
    optional exact named_service_operations selection
    provider-account prerequisites stay in Delegated to KDCube

  direct protected-service admission
    delegated bearer + independent signed service proof
    current card + active catalog + optional account-scope decision
    bounded service-scoped principal; no provider credential
```

## Implementation Rules

- Keep connection edges and delegated account connections separate in code,
  storage, docs, and UI labels.
- Keep request authenticators separate from connected accounts. A Telegram bot
  token or Slack signing secret proves requests; it is not a delegated user
  account token.
- Give every app/provider surface stable non-secret authority/authenticator ids.
  KDCube-controlled surfaces should carry them as
  `X-KDCube-Auth-Authority-ID` and `X-KDCube-Auth-Authenticator-ID`; raw
  provider-shape matching is fallback for uncontrolled hooks.
- Use `role_providing` only for authenticators that directly establish platform
  authority. Linked external providers such as Telegram normally keep it false.
- Do not decide real platform roles inside this app. Call or model a platform
  principal/role resolver after identity resolution.
- Do not grant roles because an external account exists. Roles belong to the
  platform principal.
- Do not put OAuth client secrets or user tokens in descriptor templates.
- Do not put request-authenticator secret values in Postgres or bundle-local
  state. Store only `secret_ref` metadata there; secret values stay in
  `bundles.secrets.yaml` or the configured bundle secrets provider.
- Do not treat Redis as one undifferentiated cache. Card/catalog projections
  are rebuildable; OAuth codes, refresh/access bindings, live credential
  handles, and admission nonces are TTL-bounded protocol authority.
- A direct-admission service registration binds a workload to resource
  selectors only. Operations and grants stay in the active delegated catalog.
- Keep the widget as a React/Redux app. Add slices/components instead of turning
  it into an ad hoc script.
- Keep `entrypoint.py` as shallow orchestration. Authority domain logic belongs
  in `connection-hub`; KDCube-specific request, storage, and transport bindings remain
  thin host adapters.
- Keep `interface/README.md`, the frontend docs, config templates, and journal in
  sync when changing an API or behavior.

## Runtime Checks

After backend changes:

- run Python syntax checks for changed modules;
- refresh the local KDCube runtime before testing the app through the platform.

After widget changes:

- run `npm run build` inside `ui/widgets/connections` when dependencies are
  available;
- test the widget through KDCube rather than only through Vite when validating
  auth/config propagation.
