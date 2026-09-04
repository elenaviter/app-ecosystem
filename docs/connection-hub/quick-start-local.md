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
  - ./macos-user-presence-helper.md
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

Endpoint setup performs route construction, reachability probes, and browser
open. Authorize the CLI separately through the selected deployment:

```bash
connection-hub host authorize
connection-hub host inspect
connection-hub host surfaces connection-hub@1-0
```

The browser uses the identity provider already configured by that KDCube,
including Google, Cognito, or another configured provider. The CLI creates a
PKCE verifier and an ephemeral localhost callback, discovers KDCube's
Connection Hub authorization server, and asks for a delegated caller profile
scoped to this tenant and project. The default request contains only deployment
inspection and application-surface discovery. The OAuth access and refresh
credentials remain together in the current user's native operating-system
store: macOS Keychain, Windows Credential Manager, or Linux Secret Service.

Before contacting KDCube, `host authorize` proves a disposable random
write/read/remove round trip through that credential store. A failure stops
before OAuth discovery, client registration, browser login, or card creation.
Resolve the native-store failure before continuing.

The source CLI uses the selected platform's ordinary native-store session for
these host commands. A separately signed Rust helper for keeping management
credentials behind macOS user presence is in pre-release acceptance. Its
installation does not change the ordinary command path. See
[Protect KDCube Management On macOS With User Presence](macos-user-presence-helper.md)
for the exact release gates, user installation contract, and security boundary.

When the CLI cannot launch a browser, print the short-lived authorization URL
and keep the callback waiting:

```bash
connection-hub host authorize --no-open
```

Open that URL in a browser on the same machine and complete the selected
KDCube deployment's normal login and approval flow. The callback page confirms
that the authorization response reached the CLI. Return to the terminal to
confirm token exchange and native-store custody.

Application reload is demand-driven:

```bash
connection-hub host reload connection-hub@1-0
```

When this card has no reusable reload permission, KDCube returns a consent page
bound to the exact application, invocation ID, and request digest. In an
interactive terminal the CLI opens that page, waits for approval, and retries
the unchanged request. **Allow once** admits that exact request. **Allow
always** admits later distinct reload requests while the card retains the
operation. Card changes and revocation apply to the next call.

For automation, keep the recovery machine-readable and control browser
handling explicitly:

```bash
connection-hub host reload connection-hub@1-0 \
  --invocation-id release-2026-09-03-1 \
  --no-open
```

The command exits with status `3` and returns the bounded recovery object when
authority is missing. Re-run the same command and invocation ID after the user
approves it. The selected endpoint must already be running for login,
admission, and management. Local Docker or infrastructure control recovers a
completely stopped deployment.

Disconnect when this CLI should lose its delegated authority:

```bash
connection-hub host disconnect
```

The command revokes the OAuth grant and matching delegated card before it
removes the local native-store session. A second authorization creates a new
caller profile.

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

Record the protected Connection Hub MCP endpoint shown by the application. For
the default local runtime and external-MCP gateway it is:

```bash
export CONNECTION_HUB_MCP_URL="http://localhost:5173/api/integrations/bundles/local/connection-hub/connection-hub@1-0/public/mcp/remote_mcp_proxy"
```

## 3. Choose The Caller Authorization

The governed MCP endpoint supports three local-client paths.

### Client-Owned OAuth

A client with native remote-MCP OAuth connects directly to the endpoint. Its
browser consent creates a distinct delegated card, and the client owns its
OAuth credentials. This path remains available on headless Linux or WSL when
the Connection Hub CLI cannot reach Secret Service. Continue with the direct
installation in section 4.

### OAuth-Backed Local Bridge

Create a local profile through browser Authorization Code + PKCE when the
credential should remain in the operating-system store used by the Connection
Hub CLI:

```bash
connection-hub profile authorize local-coding-agent \
  --endpoint "$CONNECTION_HUB_MCP_URL"
```

The command discovers the protected resource and authorization server, probes
the admitted endpoint, stores the complete token set in native custody, and
commits only non-secret profile metadata.

### Manually Issued Local Bridge

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

For client-owned OAuth, let `auto` verify and select the installed client's
native path:

```bash
connection-hub client install claude-code \
  --mode auto \
  --endpoint "$CONNECTION_HUB_MCP_URL"
```

Run the `authorization_command` returned by the CLI in an interactive terminal.
The same shape applies to `codex`, `hermes`, and `openclaw`. Claude Desktop
remote OAuth connectors are added in its Settings interface.

For an OAuth-backed profile created in section 3, install the local bridge:

```bash
uv tool install connection-hub-cli
connection-hub client install claude-code \
  --mode bridge \
  --profile local-coding-agent
```

For a manually issued profile, first import the one-time bearer through hidden
input, then run the same bridge installation:

```bash
connection-hub profile add local-coding-agent \
  --endpoint "$CONNECTION_HUB_MCP_URL" \
  --access-id access_example
```

Replace `claude-code` with `claude-desktop`, `codex`, `hermes`, or `openclaw`
for those clients. The generated bridge entry starts a local stdio helper. Its
credential remains in macOS Keychain, Windows Credential Manager, or Linux
Secret Service and is absent from the client configuration. See
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

Keep the external client running and keep its authorization unchanged.

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
connection-hub host inspect
connection-hub host surfaces connection-hub@1-0
connection-hub host reload connection-hub@1-0
connection-hub host stop
connection-hub host start
connection-hub host open
```

Local `start` and `stop` apply to the explicitly selected local workdir.
Delegated `inspect`, `surfaces`, and `reload` apply to any selected running
local or endpoint target after `host authorize`.

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
