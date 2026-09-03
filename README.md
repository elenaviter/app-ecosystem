# app-ecosystem

Installable applications and packages for shared application infrastructure.

A deployment may run many applications, agents, and automations. This
repository provides shared applications and Python packages for those systems.
Each component has its own documentation, tests, and release version.

## Connection Hub

Connection Hub manages delegated authority for callers that act on behalf of
a user.

Each delegated caller profile has its own live access card. The caller can be
an agent, sub-agent, automation, MCP client, service process, or another
authorized client. The authority attached to it lives in one central record:
acting for which user, on which connected accounts, which operations, which
claims, which caps, until when. A guarded boundary resolves the current
authority on every call. Calls carry only plain facts, the actor identity and
the wanted grant. Authorization evidence does not travel with calls, and an
edit or revocation of the card applies on the very next call.

Connection Hub can run as the
[`connection-hub@1-0`](products/connection-hub/apps/connection-hub@1-0/README.md) application in
[KDCube](https://github.com/kdcube/kdcube), which provides the application
server and runtime services. From there, Connection Hub serves KDCube
applications and agents, MCP clients, and registered external services.

Start the released product with
[Run Connection Hub Locally With KDCube](docs/connection-hub/quick-start-local.md).
The workflow connects a Streamable HTTP MCP, creates a separate caller profile
with exact tool and `Once` or `Always` authority, and proves live narrowing or
revocation from a running client.

The reusable Python distribution is **`connection-hub`**, imported as
`connection_hub`. It owns actor and grant references, cards and catalogs,
per-call admission, structured denials, and protected-service signing helpers.

The **`connection-hub-cli`** distribution installs the `connection-hub`
command for KDCube application-host selection, local caller profiles, macOS
Keychain custody, a stdio MCP bridge, and client registration. Its default
first run is:

```bash
uv tool install connection-hub-cli
connection-hub setup
```

See
[Connect Local MCP Clients Without Bearer Files](docs/connection-hub/local-client-helper.md).

The pre-release
[macOS user-presence helper](docs/connection-hub/macos-user-presence-helper.md)
is a separately signed Rust application for exact KDCube management operations.
It keeps the complete management OAuth session inside its provisioned Keychain
boundary and returns only secret-free evidence. It is not yet wired into the
shared `connection-hub` command.

```text
Connection Hub
├── product root: products/connection-hub
├── KDCube application: products/connection-hub/apps/connection-hub@1-0
├── Python library and client SDK: products/connection-hub/packages/connection-hub
├── local client helper: products/connection-hub/packages/connection-hub-cli
├── macOS presence helper: products/connection-hub/packages/connection-hub-presence-helper-macos
└── standalone service host: planned under products/connection-hub/services
```

## Universal harness

The harness that wraps agents so they work at scale: ordered event
delivery per conversation, a stop or a new thought reaching the agent in
the middle of its run, durable records and workspaces, native agents and
agents that run their own loop (such as LangGraph or Claude Code) under
one contract. Its packaging starts after the Connection Hub package.

## Packages And Native Components

| Component | Status | Contract |
| --- | --- | --- |
| [`connection-hub`](products/connection-hub/packages/connection-hub/README.md) | Alpha implementation | Delegated cards and catalogs, OAuth and connected-account policy, managed-boundary admission, and direct protected-service admission. |
| [`connection-hub-cli`](products/connection-hub/packages/connection-hub-cli/README.md) | Alpha implementation | KDCube host selection, local caller profiles, macOS Keychain custody, one stdio MCP bridge, and registration adapters for external MCP clients. |
| [`ConnectionHubPresenceHelper`](products/connection-hub/packages/connection-hub-presence-helper-macos/README.md) | Pre-release | Signed Rust executor for protected KDCube management OAuth sessions and three fixed request-bound operations on macOS. |
| [`app-foundation`](packages/app-foundation/README.md) | `2026.09.03.1835` implementation candidate | Host-neutral MCP client construction and normalization; further identity, storage, HTTP, event, and observability extraction follows contract by contract. |
| [`service-foundation`](packages/service-foundation/README.md) | `2026.09.03.1835` implementation candidate | Generic host-relay adapter, polling, retry, stop, health, and discovery contracts; broader standalone-service extraction remains planned. |
| [`harness-foundation`](packages/harness-foundation/README.md) | `0.0.1` planning marker | Future distributed agent-turn, event, record, and workspace harness. |
| [`economics-foundation`](packages/economics-foundation/README.md) | `2026.09.02.1559` planning marker | Future host-neutral service usage, attribution, pricing, budget admission, reservation, and settlement contracts. |

Planning markers reserve names and do not claim an implemented API. The two
foundation implementation candidates name their current source contracts but
are not releases until their release records and tags pass the documented
gate. `connection-hub` is used by the Connection Hub application now. The
native presence helper is source-complete but remains outside the supported
CLI path until its release gates pass.

## Examples

[Examples are grouped by product or component](examples/README.md). The current
Connection Hub group includes a runnable protected backend that obtains a live,
replay-protected operation decision before applying its own domain rule.

## Status

Early and public. Design and build history is journaled, and package releases
are independently versioned.
