# app-ecosystem

Installable applications and packages for shared application infrastructure.

A deployment may run many applications, agents, and automations. This
repository provides shared applications and Python packages for those systems.
Each component has its own documentation, tests, and release version.

## Connection Hub

Connection Hub manages delegated authority for agents, sub-agents,
automations, and registered external clients.

Every caller, an agent, a sub-agent, an automation, has its own identity
card. The authority attached to it lives in one central record: acting for
which user, on which connected accounts, which operations, which claims,
which caps, until when. A guarded boundary resolves the current authority
on every call. Calls carry only plain facts, the actor identity and the
wanted grant. Authorization evidence does not travel with calls, and an
edit or revocation of the card applies on the very next call.

Connection Hub can run as the
[`connection-hub@1-0`](products/connection-hub/apps/connection-hub@1-0/README.md) application in
[KDCube](https://github.com/kdcube/kdcube), which provides the application
server and runtime services. From there, Connection Hub serves KDCube
applications and agents, MCP clients, and registered external services.

The reusable Python distribution is **`connection-hub`**, imported as
`connection_hub`. It owns actor and grant references, cards and catalogs,
per-call admission, structured denials, and protected-service signing helpers.

```text
Connection Hub
├── product root: products/connection-hub
├── KDCube application: products/connection-hub/apps/connection-hub@1-0
├── Python library and client SDK: products/connection-hub/packages/connection-hub
└── standalone service host: planned under products/connection-hub/services
```

## Universal harness

The harness that wraps agents so they work at scale: ordered event
delivery per conversation, a stop or a new thought reaching the agent in
the middle of its run, durable records and workspaces, native agents and
agents that run their own loop (such as LangGraph or Claude Code) under
one contract. Its packaging starts after the Connection Hub package.

## Packages

| Distribution | Status | Contract |
| --- | --- | --- |
| [`connection-hub`](products/connection-hub/packages/connection-hub/README.md) | Alpha implementation | Delegated cards and catalogs, OAuth and connected-account policy, managed-boundary admission, and direct protected-service admission. |
| [`app-foundation`](packages/app-foundation/README.md) | `0.0.1` planning marker | Future host-neutral identity, secrets, storage, HTTP, event, and observability primitives. |
| [`service-foundation`](packages/service-foundation/README.md) | `0.0.1` planning marker | Future standalone service composition, authentication, token, config, health, and migration contracts. |
| [`harness-foundation`](packages/harness-foundation/README.md) | `0.0.1` planning marker | Future distributed agent-turn, event, record, and workspace harness. |

The planning markers reserve names; they do not claim an implemented API.
`connection-hub` is the first implemented distribution and is used by the
Connection Hub application now.

## Examples

[Examples are grouped by product or component](examples/README.md). The current
Connection Hub group includes a runnable protected backend that obtains a live,
replay-protected operation decision before applying its own domain rule.

## Status

Early and public. Design and build history is journaled, and package releases
are independently versioned.
