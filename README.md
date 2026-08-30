# app-ecosystem

Components an application ecosystem needs, shipped as installable packages.

A modern deployment runs many applications, agents, and automations side by
side. Some components are needed by all of them and deserve to exist on
their own. This repository is where those components grow as standalone
packages.

## Connection Hub

The identity provider appeared because many applications needed one
authority on who the user is. Delegated access for agents and automations
needs its own authority the same way. The Connection Hub is that component.

Every caller, an agent, a sub-agent, an automation, has its own identity
card. The authority attached to it lives in one central record: acting for
which user, on which connected accounts, which operations, which claims,
which caps, until when. A guarded boundary resolves the current authority
on every call. Calls carry only plain facts, the actor identity and the
wanted grant. Authorization evidence does not travel with calls, and an
edit or revocation of the card applies on the very next call.

Connection Hub is the product. Today it runs as the
[`connection-hub@1-0`](apps/connection-hub@1-0/README.md) application inside
[KDCube](https://github.com/kdcube/kdcube), serving its apps, agents, MCP
surfaces, and registered external services.

The product's reusable Python distribution is **`connection-hub`**, imported
as `connection_hub`. It owns actor and grant references, cards and catalogs,
per-call admission, structured denials, and protected-service signing helpers.

```text
Connection Hub
├── KDCube application: apps/connection-hub@1-0
├── Python library and client SDK: packages/connection-hub
└── standalone service host: planned as services/connection-hub
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
| [`connection-hub`](packages/connection-hub/README.md) | Alpha implementation | Delegated cards and catalogs, OAuth and connected-account policy, managed-boundary admission, and direct protected-service admission. |
| [`app-foundation`](packages/app-foundation/README.md) | `0.0.1` planning marker | Future host-neutral identity, secrets, storage, HTTP, event, and observability primitives. |
| [`service-foundation`](packages/service-foundation/README.md) | `0.0.1` planning marker | Future standalone service composition, authentication, token, config, health, and migration contracts. |
| [`harness-foundation`](packages/harness-foundation/README.md) | `0.0.1` planning marker | Future distributed agent-turn, event, record, and workspace harness. |

The planning markers reserve names without pretending their intended APIs have
shipped. `connection-hub` is the first implemented distribution and is used by
the Connection Hub application now.

## Examples

[Examples are grouped by product or component](examples/README.md). The current
Connection Hub group includes a runnable protected backend that obtains a live,
replay-protected operation decision before applying its own domain rule.

## Status

Early and public. Design and build history is journaled, package releases are
independently versioned, and implementation moves out of KDCube only behind
verified compatibility boundaries.
