# Connection Hub

One authority for delegated access in an ecosystem of services, agents, and
automations.

The identity provider exists because many applications need one authority on
who the user is. Delegated access needs its own authority the same way: when
agents, automations, and external tools act on a user's behalf against many
services, someone has to hold the answer to "what may this caller do right
now, for which user, on which accounts". Connection Hub is that component.

Every caller gets its own **delegated-access card**: a live, versioned record
of whose authority it uses, which resources and operations it may reach,
which connected accounts it may touch, and until when. The deployment
publishes an **active capability catalog**: the ceiling of what may be
delegated at all. A guarded service resolves *the current card intersected
with the active catalog* on **every call**. Authorization never travels
inside tokens, so there is nothing to copy between executions, and an edit
or revocation applies on the very next call.

Each granted operation can also be **always** available or limited to **one**
invocation. That usage policy is a separate live record, so consuming one use
does not rewrite the user's card.

```text
user (grantor) --edits/revokes--> +----------- Connection Hub -----------+
                                  | delegated cards: one per caller,     |
                                  | versioned, with a current pointer    |
                                  | active capability catalog: the       |
                                  | deployment ceiling, versioned        |
                                  +------------------+-------------------+
                                                     | resolved per call:
caller --opaque bearer--> guarded operation boundary | bearer x current card
                                                     | x active catalog
                                  allow with bounded authority, or a
                                  structured denial the caller can act on
```

This package is for both sides of that boundary:

- **service owners** who want their operations admitted through the hub:
  registration, per-call admission, workload proof, structured denials;
- **client and agent authors** whose code calls guarded services: the client
  SDK, grant checks, one-call tokens, denial-driven consent.

The package is the portable core: contracts, state machines, admission
evaluation, OAuth protocol builders, the client SDK. A host supplies HTTP
routes, sessions, storage, and secret resolution. [KDCube](https://github.com/kdcube/kdcube)
is the first host (the complete Connection Hub application ships in this
repository as [`connection-hub@1-0`](https://github.com/elenaviter/app-ecosystem/blob/main/products/connection-hub/apps/connection-hub@1-0/README.md));
a standalone service host is planned.

## Install

```bash
python -m pip install connection-hub
```

`2026.09.02.0515` adds owner-configured OAuth for external MCP connectors.
The browser flow uses MCP protected-resource and authorization-server
discovery, PKCE, Client ID Metadata Documents or dynamic registration,
server-side token storage, serialized refresh, and upstream revocation. The
existing transport protocol remains compatible for direct-credential hosts.

## Flow 1: a guarded service registers itself and admits calls

A backend that wants the hub to regulate its admissions does three things.

**Register once** (configuration on the hub, not code): declare the
capability rows the service exposes (grants, resources, operations) in the
hub's delegated catalog, and register the service as a workload: a service
id, a secret reference, and selectors of the declared resources. The
registration does not restate operations; the catalog stays the single
vocabulary both sides speak.

**Per request, ask for admission.** The caller arrives with an opaque
delegated bearer. The service builds the semantic request, signs it with its
own workload secret (possession of a user bearer is not service identity),
and asks the hub:

```python
import secrets, time

from connection_hub.delegated_credentials.admission import (
    AdmissionRequest,
    sign_admission_request,
)

request = AdmissionRequest(
    resource="https://api.example.test/customers",
    operation="customers.search",
)
timestamp = str(int(time.time()))
nonce = secrets.token_urlsafe(24)
signature = sign_admission_request(
    secret=service_signing_secret,
    service_id="crm-api",
    timestamp=timestamp,
    nonce=nonce,
    delegated_token=user_delegated_bearer,
    request=request,
)
# POST request.signing_dict() to the hub's admission endpoint with
# Authorization: Bearer <user_delegated_bearer> and the four
# X-Connection-Hub-* proof headers (service id, timestamp, nonce, signature).
```

**Enforce the decision.** An allow carries a pairwise service-scoped user id, a
separate pairwise caller-profile id, and only the bounded authority for that
one operation. It never carries provider credentials, raw card/client ids, or
the platform's internal user id. A denial is structured and actionable:

```json
{
  "ok": false, "allowed": false,
  "schema": "connection_hub.delegated_admission.v1",
  "error": {"code": "delegated_capability_not_granted",
             "message": "...", "retryable": false},
  "ret": {"reason": "delegated_capability_not_granted",
           "details": {"resource": "...", "claims": ["..."]}}
}
```

The service still applies its own domain rules after admission; the hub
answers delegation, the service answers business.

For a `once` operation, the service supplies a stable invocation id and request
digest. Connection Hub can replay its admission decision. A service that
changes domain state also records the effect under that invocation id because
the effect happens outside Connection Hub.

Runnable end to end: the
[`direct-admission-service` example](https://github.com/elenaviter/app-ecosystem/blob/main/examples/connection-hub/direct-admission-service/README.md)
and the
[deployment recipe](https://github.com/elenaviter/app-ecosystem/blob/main/docs/connection-hub/recipes/direct-protected-service.md).

## Flow 2: an external client connects through OAuth

When an external agent client (Claude Code through its connector, or any MCP
client) reaches a guarded service, the hub runs the handshake:

```text
external client                       Connection Hub host
  | 1. call the service URL without a credential
  |    <- 401 + WWW-Authenticate, protected-resource metadata (RFC 9728)
  | 2. fetch authorization-server metadata (RFC 8414; also served as
  |    openid-configuration so strict clients proceed)
  | 3. identify: pre-registered client, client-id URL (CIMD), or
  |    dynamic registration (DCR)
  | 4. authorize with PKCE (S256 required)
  |    -> the USER logs in, consents per capability, and binds the
  |       exact connected accounts the client may use
  | 5. exchange the single-use code (60 s) for an opaque bearer (1 h,
  |    rotating refresh); a delegated card is created for this client
  | 6. every subsequent call resolves the current card and active
  |    catalog; nothing is decided from the token alone
  | 7. the user edits or revokes the card at any time; the very next
  |    call obeys (RFC 7009 revocation retires the card too)
```

The consent screen is not a blanket yes: the user selects capabilities and
binds specific provider accounts, and that selection becomes the card. Full
protocol, descriptor contract, and failure modes:
[OAuth delegated credential protocol](https://github.com/elenaviter/app-ecosystem/blob/main/docs/connection-hub/package/oauth-delegated-credential-protocol.md).

## Flow 3: proxying a user-owned external MCP service

A remote MCP server does not need a Connection Hub integration. Its owner adds
the streamable-HTTP endpoint and optional upstream bearer/header credential to
Connection Hub. The credential stays in the server-side owner secret store.
Discovery creates an owner-scoped resource whose exact accepted tools can be
selected on each caller card.

The delegated caller connects to the Connection Hub `remote_mcp_proxy` with its
own opaque bearer. The proxy lists only that card's selected tools, compares
the live tool descriptor with the accepted descriptor, applies `once` or
`always`, injects the upstream credential, and calls the remote server. Because
the proxy performs the call, a retry with the same invocation id and arguments
can return the stored terminal result without redispatch.

## Flow 4: calling from code, the client SDK

A hosted agent or application talks to the hub through `ConnectionsClient`
over a one-method host transport:

```python
from connection_hub import ConnectionsClient

client = ConnectionsClient(transport)  # transport: async call(operation, payload)

# what is connected, and what may this caller do
entries = await client.catalog()                       # list[CatalogEntry]
check = await client.agent_grant_check(
    client_id="kdcube-agent:myapp:myagent",
    namespace="mem", operation="object.search",
)                                                       # governed/granted + claims

# one-call credentials, resolved by the hub, never stored by the caller
token = await client.get_token("google", account_id="acc-1")
bearer = await client.agent_grant_token(
    client_id="kdcube-agent:myapp:myagent",
    resource="https://host/api/public/mcp/named_services",
)                                                       # None while consent is pending
```

When a call is denied because a consent is missing, the denial carries
everything needed to recover: which permission, which provider, which
account, and where the user grants it. The
[`mcp_consent`](https://github.com/elenaviter/app-ecosystem/blob/main/products/connection-hub/packages/connection-hub/src/connection_hub/mcp_consent.py)
module turns such denials into a structured ask the agent can surface and
retry after the grant lands.

## What the package owns, and what a host supplies

The package owns portable authority semantics: versioned cards and catalogs,
once-or-always invocation policy and idempotency records, external MCP
connector/proxy contracts, per-call admission for managed REST, MCP, and
named-service calls, the OAuth protocol builders, connected-account policy,
structured denials, the client SDK, and explicit ports for storage, identity,
dispatch, secrets, and live delivery. The host supplies the HTTP surfaces,
authenticated sessions, durable storage and its Redis projections, secret
resolution, and the UI.
The boundary is documented in
[package extraction architecture](https://github.com/elenaviter/app-ecosystem/blob/main/docs/connection-hub/package/extraction-architecture.md).

## Documentation

- [Configuration and capabilities overview](https://github.com/elenaviter/app-ecosystem/blob/main/docs/connection-hub/configuration-and-capabilities.md)
- [Connection Hub architecture and semantic requirements](https://github.com/elenaviter/app-ecosystem/blob/main/docs/connection-hub/connection-hub-architecture.md)
- [Delegated authority and admission](https://github.com/elenaviter/app-ecosystem/blob/main/docs/connection-hub/package/delegated-authority-and-admission.md)
- [Delegated access cards](https://github.com/elenaviter/app-ecosystem/blob/main/docs/connection-hub/package/delegated-cards.md)
- [OAuth delegated credential protocol](https://github.com/elenaviter/app-ecosystem/blob/main/docs/connection-hub/package/oauth-delegated-credential-protocol.md)
- [Direct protected-service recipe](https://github.com/elenaviter/app-ecosystem/blob/main/docs/connection-hub/recipes/direct-protected-service.md)
- [Release procedure](https://github.com/elenaviter/app-ecosystem/blob/main/docs/releases.md)

License: MIT. Source: https://github.com/elenaviter/app-ecosystem
