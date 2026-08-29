---
id: prokura/frontend/application/storage
title: "Connection Hub Storage Map"
summary: "Physical storage map for connection-hub@1-0: descriptor and secret authority, shared app storage, user-scoped account state, Postgres metadata, and Redis durable-projection and live-protocol roles."
status: active
tags: ["connection-hub", "storage", "secrets", "postgres", "identity", "authenticators", "connections"]
keywords: ["delegated card storage", "capability catalog storage", "connected account secrets", "OAuth grant store", "Redis authority"]
see_also:
  - ../../../connection-hub-architecture.md
  - ../../../package/delegated-cards.md
  - ../../../../../apps/connection-hub@1-0/interface/README.md
---

# Connection Hub Storage Map

This page owns the physical storage map. The semantic distinctions and surface
architecture are canonical in
[Connection Hub Architecture And Semantic Requirements](../../../connection-hub-architecture.md).

Connection Hub uses several storage surfaces on purpose. Do not collapse them
into one "connection store" or treat all Redis records as cache:

```text
bundles.yaml / descriptor authority
  non-secret deployment config:
    providers/apps, identity settings, active catalog source,
    descriptor authenticators, protected-service registrations

bundles.secrets.yaml / bundle secrets provider
  deployment secrets:
    OAuth client secrets, Telegram bot tokens, signing secrets,
    protected-service HMAC secrets, pairwise projection secret

Postgres
  request-authenticator metadata managed by the widget/admin API
    provider, row id, selector/verifier hints, secret_ref

shared app storage
  connection edges and link challenges in the current implementation
  immutable delegated-card revisions + current pointers
  immutable delegated-catalog versions + active pointer

user properties + user secrets
  connected-account metadata in properties
  provider access/refresh tokens and app-passwords in server-side user secrets

Redis
  live OAuth codes, clients, refresh/access bindings, credential handles
  replay/CSRF state, catalog/card projections, selector cache,
  named-service discovery, event delivery, coordination
```

The security rule is strict: **Connection Hub metadata may reference a secret,
but must never store the secret value.** Secret values are read through the
bundle secret lifecycle with `get_secret("b:<path>")`.

## Ownership Matrix

| Object | Owner | Storage | Contains secrets? | Notes |
| --- | --- | --- | ---: | --- |
| Delegated integration connector app config | operator/admin | `bundles.yaml` effective app props | no | `connections.delegated_to_kdcube.providers.<provider>.connector_apps`, `identity.authenticators[]`, visibility config. |
| OAuth connector app secret | operator/admin | `bundles.secrets.yaml` or configured bundle secrets provider | yes | `connections.delegated_to_kdcube.providers.<provider>.connector_apps.<connector_app_id>.client_secret`. |
| Telegram bot token | operator/admin | `bundles.secrets.yaml` or configured bundle secrets provider | yes | Referenced from `identity.authenticators[].secret_ref`, e.g. `identity.authenticators.telegram_kdcube_ref.bot_token`. |
| Request-authenticator row | Connection Hub | Postgres | no | `connection_hub_request_authenticators`; stores metadata and `secret_ref` only. |
| Identity link | Connection Hub | shared app storage | no | Current implementation uses `connections/connection-edges.json`; maps a verified authority subject to a platform user. |
| Identity-link challenge | Connection Hub | shared app storage | no | Current implementation uses `connections/connection-edge-challenges.json` for short-lived proof flows. |
| Delegated card revision and current pointer | Prokura delegated authority | shared app storage | no | Immutable revisions under `delegated-cards/v1`; current pointer is the durable live authority. |
| Capability catalog version and active pointer | Prokura delegated authority | shared app storage | no | Immutable versions plus self-contained `active.json` under `delegated-catalog/v1`. |
| Connected-account metadata | Connection Hub delegated-to-KDCube | user properties | no | Provider, connector app, external subject, claims, status, and credential handle. |
| Connected-account credential | Connection Hub delegated-to-KDCube | server-side user secrets | yes | OAuth tokens and app-passwords use the same broker contract. They are never stored in descriptors or returned to browsers. |
| OAuth authorization code, refresh record, and access-grant binding | Prokura delegated OAuth | Redis | yes, bounded protocol state | Codes are single use; refresh records rotate; opaque access tokens resolve through a hashed grant binding and current card pointer. |
| Delegated-card credential handle | Prokura delegated authority | Redis | yes, bounded live handle | Kept separately from durable card authority and expires with the card/credential. |
| Card/catalog serving projection | Prokura delegated authority | Redis | no | Rebuildable from committed shared app storage; read-through restores missing projections. |
| Direct-admission nonce | Prokura direct admission | Redis | no | Single-use `(service_id, nonce)` replay record; admission fails closed when this store is unavailable. |
| Protected-service registration | operator/admin | effective app props | no | Binds a service id and `secret_ref` to catalog resource selectors only. |
| Protected-service signing and identity-projection secrets | operator/admin | app secret provider | yes | Per-service HMAC secret plus separate deployment projection secret, each at least 32 bytes. |

## Request-Authenticator Metadata

The widget-managed request-authenticator table is provisioned by
`prokura.hub.authenticator_store` in
the tenant/project Postgres schema:

```text
<schema>.connection_hub_request_authenticators
  authenticator_id   text primary key
  tenant             text
  project            text
  bundle_id          text
  provider           text
  authority_id       text
  connection_id      text
  label              text
  enabled            boolean
  role_providing     boolean
  subject_namespace  text
  secret_ref         text
  selector           jsonb
  verifier           jsonb
  properties         jsonb
  created_at         timestamptz
  updated_at         timestamptz
  deleted_at         timestamptz
```

Example row:

```json
{
  "authenticator_id": "telegram.support",
  "provider": "telegram",
  "authority_id": "telegram.support",
  "connection_id": "telegram.support",
  "label": "Support bot",
  "enabled": true,
  "role_providing": false,
  "subject_namespace": "telegram",
  "secret_ref": "identity.authenticators.telegram_support.bot_token",
  "selector": {},
  "verifier": {},
  "properties": {
    "where": "built-in",
    "definition": {
      "bot_username": "support_bot"
    }
  }
}
```

`authority_id` is the identity/grant realm. KDCube-controlled surfaces should
carry `X-KDCube-Auth-Authority-ID` and `X-KDCube-Auth-Authenticator-ID`.
`role_providing` should be
`false` for linked external providers such as Telegram; platform roles come
from the linked platform principal, not from the Telegram-local role.

The corresponding secret value belongs in bundle secrets:

```yaml
bundles:
  items:
    - id: connection-hub@1-0
      secrets:
        identity:
          telegram:
            bot_token_support: "<TELEGRAM_BOT_TOKEN>"
```

`authenticators_upsert` rejects payloads that contain secret value fields such
as `secret_value`, `bot_token`, `client_secret`, `signing_secret`, or `api_key`.
The API accepts only `secret_ref`.

## Descriptor-Defined Authenticators

Operators can also define immutable deployment rows in app config:

```yaml
identity:
  authenticators:
    - id: telegram.kdcube_ref
      provider: telegram
      authority_id: telegram.kdcube_ref
      where: built-in
      enabled: true
      role_providing: false
      secret_ref: identity.authenticators.telegram_kdcube_ref.bot_token
      definition:
        label: KDCube Ref Telegram bot
        bot_name: kdcube-ref
        bot_username: kdcube_doc_bot
        web_app_auth_max_age_seconds: 86400
```

For compatibility, the app also reads:

```yaml
identity:
  telegram:
    authenticators:
      - id: telegram.kdcube_ref
        provider: telegram
        secret_ref: identity.authenticators.telegram_kdcube_ref.bot_token
        enabled: true
```

Runtime rows are the merge of descriptor rows and Postgres rows. When ids
collide, the Postgres row wins for runtime behavior. The UI marks descriptor
rows as `source=config` and Postgres rows as `source=postgres`.

## Connection Edges And Challenges

Identity links are the authority bridge:

```text
verified provider subject
  telegram:100200300
      |
      v
platform user id
  a1b2c3d4-...
```

The current implementation keeps connection edges and one-time challenges in
shared app storage through `prokura.hub.edges`.
Those records do not contain platform roles. Role/economics authority is
resolved after the link points at a platform principal.

The edge-store interface is replaceable. A host can move edges and challenges
to another durable store without changing their semantic contract. Do not move
secret values into the edge records.

## Delegated Accounts

Delegated account connections are separate from connection edges:

```text
platform user id
  -> Gmail/Slack/iCloud account token
```

These tokens let automation act on a user's connected account. They do not prove
platform identity and must not grant platform roles.

The portable Prokura store splits these records deliberately:

```text
user properties
  delegated_to_kdcube.account_index
  delegated_to_kdcube.accounts.<account-id>

server-side user secrets
  delegated_to_kdcube.credentials.<credential-id>
```

KDCube supplies the user-property and user-secret backend. Deployment OAuth
connector app secrets stay in app secrets and are not copied into user records.

## Delegated Cards And Catalog

Cards and the catalog are durable, versioned authority under the app storage
root:

```text
delegated-cards/v1/grantors/<subject-hash>/cards/<access-id>/
  revisions/card_revision_<stamp>_<revision>_<hash>.json
  current.json

delegated-catalog/v1/
  versions/<catalog-version>.json
  active.json
```

Card and catalog Redis rows are serving projections. A reader restores a
missing projection from the committed revision or version. Publishing and
editing commit durable state before projecting it.

Credential handles are intentionally separate from card authority. A durable
card says what may be done; a bounded live handle is still required to prove a
current caller.

## Redis: Projection, Protocol State, And Coordination

Redis has three roles:

```text
rebuildable projections
  active card/catalog projections, authenticator selector cache

TTL-bounded protocol authority
  OAuth authorization codes, dynamic client registrations, refresh records,
  access-token grant bindings, card credential handles, consent CSRF,
  direct-admission replay nonces

coordination and delivery
  discovery, events, locks, pending demand/event state
```

The second group is not reconstructable cache. Losing it invalidates the
affected OAuth credential or in-flight protocol. It must never produce an
implicit allow. Durable card/catalog state can restore authority documents,
but it cannot recreate a lost bearer binding or credential handle.

Connection Hub also uses Redis as a short-lived selector cache for
request-authenticator metadata:

```text
identity.authenticator_selector_cache
  enabled: true
  ttl_seconds: 30
```

The authenticator selector cached payload is only the merged metadata rows used for
candidate selection. It does not cache Telegram proof validation, connection-edge
resolution, platform roles, delegated-account tokens, or authorization results.
`authenticators_upsert`, `authenticators_remove`, and descriptor bootstrap
invalidate the cache.

## Direct Admission State

Direct protected-service admission adds no card or catalog store. It reads the
same access binding, current card, and active catalog used by managed REST/MCP
guards. Its additional state is:

- descriptor-owned service id, resource selectors, and `secret_ref`;
- per-service signing secret in app secrets;
- deployment pairwise-subject projection secret in app secrets;
- Redis replay nonce with bounded TTL;
- ordinary runtime decision logs without the bearer or secret values.

See the
[direct protected-service admission contract](../../../connection-hub-architecture.md#direct-protected-service-admission)
for the wire protocol and trust boundary.
