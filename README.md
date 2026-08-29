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

Today the Connection Hub runs inside [KDCube](https://github.com/kdcube/kdcube),
serving its apps, agents, and MCP surfaces. The packages published from
here make it available on its own:

The packaged, standalone form of the Connection Hub ships under the name
**Prokura**: the old commercial-law institution of delegated signing
authority that lives in a register, verified against the register rather
than any carried letter, revocable at the register.

- **`prokura`** (first): the boundary and client SDK. The actor and
  grant reference format, per-call admission, structured denials a caller
  can act on, and helpers for a service to publish its operations and
  claims, per account where connected accounts are involved.
- The standalone Prokura service follows.

## Universal harness

The harness that wraps agents so they work at scale: ordered event
delivery per conversation, a stop or a new thought reaching the agent in
the middle of its run, durable records and workspaces, native agents and
agents that run their own loop (such as LangGraph or Claude Code) under
one contract. Its packaging starts after `prokura`.

## Packages

| Distribution | Status | Contract |
| --- | --- | --- |
| [`prokura`](packages/prokura/README.md) | Alpha implementation | Delegated cards and catalogs, OAuth and connected-account policy, managed-boundary admission, and direct protected-service admission. |
| [`app-foundation`](packages/app-foundation/README.md) | `0.0.1` planning marker | Future host-neutral identity, secrets, storage, HTTP, event, and observability primitives. |
| [`service-foundation`](packages/service-foundation/README.md) | `0.0.1` planning marker | Future standalone service composition, authentication, token, config, health, and migration contracts. |
| [`harness-foundation`](packages/harness-foundation/README.md) | `0.0.1` planning marker | Future distributed agent-turn, event, record, and workspace harness. |

The planning markers reserve names without pretending their intended APIs have
shipped. Prokura is the first implemented package and is consumed by the
Connection Hub application now.

## Examples

- [Direct-admission protected service](examples/direct-admission-service/README.md)
  shows a backend outside KDCube accepting an opaque delegated bearer and
  obtaining a live, replay-protected operation decision from Connection Hub.

## Status

Early and public. Design and build history is journaled, package releases are
independently versioned, and implementation moves out of KDCube only behind
verified compatibility boundaries.
