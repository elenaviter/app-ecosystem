---
id: connection-hub/quick-start-local
title: Run Connection Hub Locally With KDCube
summary: Install the released KDCube host, connect an external MCP to Connection Hub, issue one bounded caller credential, and prove live narrowing or revocation from an external client.
status: active
tags: [connection-hub, quickstart, localhost, kdcube, mcp, delegated-access]
keywords: [Connection Hub local install, external MCP proxy, automation credential, remote MCP client, provisioned OAuth]
see_also:
  - ./README.md
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

Prerequisites are Docker Engine or Docker Desktop, Python 3.9 or newer, Git,
and about 20 GB of free disk space.

```bash
python3 -m pip install --upgrade kdcube-cli
kdcube init --tenant local --project hub --auth-type simple
kdcube start --tenant local --project hub
```

`simple` is the local development login. Use the default Google login or an
existing identity provider for a shared deployment. The CLI prints the actual
browser URL; the shipped local default is:

```text
http://localhost:5173/platform/chat
```

The runtime state lives under:

```text
~/.kdcube/kdcube-runtime/local__hub/
```

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

Configure any Streamable HTTP MCP client with the endpoint shown by Connection
Hub and this request header:

```text
Authorization: Bearer <one-time-shown-caller-bearer>
```

Client configuration syntax differs, but the two values are the same for
Claude Desktop, Claude Code, Hermes, OpenClaw, and other Streamable HTTP MCP
clients. Clients with OAuth support can instead connect to the endpoint without
a copied bearer: Connection Hub's caller OAuth flow signs in the owner and
creates a separate OAuth-managed card for that client.

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
kdcube info --tenant local --project hub
kdcube stop --tenant local --project hub
kdcube start --tenant local --project hub
kdcube refresh --tenant local --project hub --latest
```

Configuration belongs in the staged descriptors under `config/`. Application
and user state belongs under `data/`; diagnostics belong under `logs/`. Back up
the whole `local__hub` workdir while the runtime is stopped when the local
Connection Hub records must survive machine loss. Do not repair generated
Redis or container files manually.

Public HTTPS endpoints work with the default outbound policy. HTTP and private
network targets are deployment policy and stay disabled unless an operator
explicitly enables the exact local-development exception in the Connection Hub
descriptor. See the full
[end-to-end acceptance procedure](testing/end-to-end-acceptance.md) for
two-owner isolation, descriptor drift, restart durability, OAuth refresh, and
secret/log scanning.
