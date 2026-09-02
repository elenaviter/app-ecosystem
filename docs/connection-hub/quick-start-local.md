---
id: connection-hub/quick-start-local
title: Run Connection Hub Locally With KDCube
summary: Install the released KDCube host, connect an external MCP to Connection Hub, issue one bounded caller credential, and prove live narrowing or revocation from an external client.
status: active
tags: [connection-hub, quickstart, localhost, kdcube, mcp, delegated-access]
keywords: [Connection Hub local install, external MCP proxy, automation credential, remote MCP client, provisioned OAuth]
see_also:
  - ./README.md
  - ./local-client-helper.md
  - ./connection-hub-architecture.md
  - ./testing/end-to-end-acceptance.md
  - https://github.com/kdcube/kdcube/blob/main/app/ai-app/docs/quick-start-README.md
---

# Run Connection Hub Locally With KDCube

This path starts the released Connection Hub application inside a local KDCube
runtime, connects one remote MCP service, and gives an external agent access to
selected tools through a revocable caller profile. The remote service receives
its own upstream credential; the agent receives a different Connection Hub
credential for its profile.

## 1. Start The Local Host

Prerequisites are Docker Engine or Docker Desktop, Python 3.10 or newer, Git,
`uv`, and about 20 GB of free disk space.

KDCube uses Google login by default. Create a Google OAuth client of type
**Web application**, add `http://localhost:5173` as an authorized JavaScript
origin, and keep its public client ID ready. This browser login does not use a
Google client secret.

```bash
uv tool install connection-hub-cli
connection-hub setup
```

The setup command asks for the Google Web client ID and an optional verified
Google email that receives bootstrap platform administration. It uses the
compatible `kdcube-cli` library installed with the product command, selects the
latest released KDCube source, creates a dedicated `local` / `connection-hub`
runtime, stages descriptor-owned Google session authentication, starts the
runtime, verifies both Connection Hub routes, and opens the browser
application. It does not invoke another CLI or duplicate KDCube's deployment
logic.

For non-interactive setup, provide the Google values explicitly:

```bash
connection-hub setup \
  --google-client-id "$GOOGLE_CLIENT_ID" \
  --bootstrap-admin-email "admin@example.com"
```

The public Google client ID is written to the staged descriptor. To select the
development-only SimpleIDP login explicitly:

```bash
connection-hub setup --auth simple
```

The CLI returns and opens the direct Connection Hub widget URL. With the
default coordinates it is:

```text
http://localhost:5173/api/integrations/bundles/local/connection-hub/connection-hub@1-0/public/widgets/connections_settings
```

The canonical dedicated workdir is:

```text
~/.kdcube/kdcube-runtime/local__connection_hub/
```

To use an explicitly selected KDCube runtime instead:

```bash
connection-hub setup \
  --local-workdir ~/.kdcube/kdcube-runtime/demo-tenant__demo-project
```

To use Connection Hub already deployed elsewhere, provide the KDCube public
origin and its route coordinates:

```bash
connection-hub setup \
  --endpoint https://runtime.example \
  --tenant acme \
  --project production
```

An endpoint can use a loopback name or address, an IP address, or a DNS name.
Plain HTTP is accepted only on loopback; IP and DNS endpoints require HTTPS.
Endpoint setup resolves and probes the existing browser and MCP routes, records
their non-secret coordinates, and opens the browser application. The user then
signs in through that deployment's configured identity provider.

In the current release, endpoint setup performs route construction,
reachability probes, and browser open. It does not yet authorize the CLI as a
remote management caller, so endpoint targets do not currently support start,
stop, configuration, reload, or logs.

The remote-management extension uses Connection Hub's existing delegated-card
model. The user signs in to the target KDCube in the browser and approves a CLI
caller profile containing selected KDCube management resources and operations.
The CLI receives a credential bound to that card through Authorization Code
with PKCE and keeps it in the operating-system credential store. A person,
agent, or automation may then invoke the CLI; KDCube resolves the card on every
management call. Missing operations can request user consent, and edits or
revocation take effect on the next call. The endpoint must already be running
for login and admission; recovery of a completely stopped deployment uses its
local or infrastructure control plane.

KDCube is the host in this workflow. It supplies login, durable app and user
storage, server-side secrets, locking, routes, and lifecycle. This quick start
does not start a standalone Connection Hub process.

## 2. Connect The Remote MCP

Sign in, open **Connection Hub**, then open **External MCP**. Enter a name and
the service's Streamable HTTP endpoint. Select the upstream authentication the
service requires:

- **No credential** for a public MCP endpoint;
- **Bearer token** or **Custom header** for a provider-issued static secret;
- **OAuth browser login** for an OAuth-protected MCP endpoint.

OAuth offers two client-registration choices:

- **Automatic registration** uses a Client ID Metadata Document when supported,
  otherwise dynamic client registration;
- **Client created in provider console** uses an OAuth client you registered at
  the provider.

For the provider-console path, first copy the exact **Redirect URI** shown by
Connection Hub into the provider's OAuth client configuration. Then enter the
provider-issued client id, choose its configured token-endpoint authentication
method, enter the client secret when required, and select **Authorize MCP
server**. Protected-resource and authorization-server discovery, PKCE, scope,
issuer checks, callback state, token exchange, refresh, and revocation remain
active for both registration choices.

The client secret is submitted once into the owner-scoped OAuth transaction and
then kept with the connector credential in server-side user-secret storage. It
does not enter the connector record, descriptor, delegated card, browser
response, tool list, or tool result. **Reconnect** reuses that client. **OAuth
client** explicitly replaces it.

When discovery completes, inspect the exact tools and accept any reviewed
descriptor change. Newly discovered tools do not become caller permissions by
being discovered.

## 3. Create A Caller Profile

Open **Delegated by KDCube**, then **Create automation access**.

1. Give the caller a name, such as `local-coding-agent`.
2. Select the external MCP connector resource.
3. Select only the tools this caller needs.
4. Choose **Always** or **Once** for each selected operation.
5. Create the access.

Connection Hub shows two client values:

- the exact `remote_mcp_proxy` Streamable HTTP endpoint;
- the new caller bearer, shown once.

Store the bearer in that client's private credential store, secret manager, or
runtime environment. Keep it out of repositories, prompts, command history,
and logs. The bearer identifies this caller profile; it is not the remote
service's credential.

## 4. Connect An External MCP Client

For a local MCP client on macOS, install the Connection Hub helper and import
the one-time-shown bearer through hidden input:

```bash
uv tool install connection-hub-cli
connection-hub profile add local-coding-agent
connection-hub client install claude-code --profile local-coding-agent
```

Replace `claude-code` with `claude-desktop`, `hermes`, or `openclaw` for those
clients. The generated client entry starts a local stdio helper. The bearer
stays in macOS Keychain and is absent from the client configuration. See
[Connect Local MCP Clients Without Bearer Files](local-client-helper.md) for
the complete commands, diagnostics, generic-client entry, and removal
semantics.

Alternatively, configure any Streamable HTTP MCP client directly with the
endpoint shown by Connection Hub and this request header:

```text
Authorization: Bearer <one-time-shown-caller-bearer>
```

Client configuration syntax differs. Clients with OAuth support can instead
connect to the endpoint without a copied bearer: Connection Hub's caller OAuth
flow signs in the owner and creates a separate OAuth-managed card for that
client.

List tools, then call one selected tool. The client sees only tools selected on
its current profile. Connection Hub resolves the profile, connector, accepted
tool descriptor, invocation policy, and upstream credential on every call.

## 5. Prove Live Enforcement

Keep the external client running and keep its bearer unchanged.

1. Return to **Delegated by KDCube** and remove one tool from the profile.
2. Ask the client to list or call that tool again.
3. Confirm it disappears or returns a structured authorization denial.
4. Add it back with **Once**, then call it successfully once.
5. Call it again with a new request and confirm the invocation is exhausted.
6. Revoke the caller profile and confirm its next request fails immediately.

The proof is that authority changes at Connection Hub while the client keeps
the same credential and process. A new local script cannot expand that
authority because every exposed operation resolves the current profile.

## 6. Operate The Local Runtime

```bash
connection-hub status
connection-hub doctor --probe
connection-hub host status --probe
connection-hub host stop
connection-hub host start
connection-hub host open
```

Lifecycle commands apply only to the explicitly selected local workdir.
Endpoint targets can be inspected, probed, and opened but are administered
through their deployment's own operator channel.

Configuration belongs in the local workdir's staged descriptors under
`config/`. Application and user state belongs under `data/`; diagnostics
belongs under `logs/`. Back up the whole workdir while the runtime is stopped
when the local Connection Hub records must survive machine loss. Do not repair
generated Redis or container files manually.

Public HTTPS endpoints work with the default outbound policy. HTTP and private
network targets are deployment policy and stay disabled unless an operator
explicitly enables the exact local-development exception in the Connection Hub
descriptor. See the full
[end-to-end acceptance procedure](testing/end-to-end-acceptance.md) for
two-owner isolation, descriptor drift, restart durability, OAuth refresh, and
secret/log scanning.
