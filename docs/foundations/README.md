# Shared Foundation Boundaries

The shared foundations move reusable realms out of a monorepo without moving
product policy with them. Extraction follows ownership: a neutral mechanism
moves first, its old import becomes a compatibility facade, and host or product
adapters retain the semantics that make it specific.

## Dependency Direction

The first implemented slices are independent sibling packages:

```text
KDCube MCP runtime -------- depends on -------> app-foundation[mcp]
Connection Hub CLI -------- depends on -------> app-foundation[mcp]

Problem Board host command - depends on ------> Connection Hub CLI
Problem Board host command - depends on ------> service-foundation

app-foundation             - does not import -> service-foundation
service-foundation         - does not import -> app-foundation
either foundation          - does not import -> a product or KDCube
```

The product or application is the composition layer. Shared foundations do not
reach sideways into each other merely because one use case needs both.

## Why MCP Starts In `app-foundation`

The extracted MCP slice is a reusable outbound protocol client. It can be used
by an application, a command-line helper, or a platform runtime without owning
a standalone service process. That makes it an application-facing mechanism:

```text
endpoint + caller-supplied headers
              |
              v
app_foundation.mcp.open_mcp_client
  -> stdio | SSE | Streamable HTTP
  -> modern discovery with legacy fallback
  -> stable tool schema and result values
```

Credential stores, delegated cards, consent, and operation policy remain in
their owning products. KDCube's agent-tool adapter and distributed MCP server
defaults remain in KDCube because they express platform behavior rather than a
neutral protocol client.

## Why Host Relay Lives In `service-foundation`

A host relay is process lifecycle around one supplied adapter. Its reusable
part is polling, retry, stop, health, and adapter discovery:

```text
product composition root
  -> opens product-owned transport and credentials
  -> constructs domain adapter
  -> gives adapter to HostRelayRuntime
       -> poll_once
       -> health / retry / stop
```

For example, the Problem Board local command composes two siblings. Connection
Hub resolves a local caller profile and credential; the Problem Board adapter
translates named-service operations; `service-foundation` runs its lifecycle.
No foundation knows that complete use case.

## Migration Rule

An extracted contract has one implementation owner. The previous monorepo path
may re-export it for compatibility, but it must not retain a second copy. A
later slice moves only after its dependencies and host-specific policy have
been identified and tested independently.

## Release Order

`app-foundation` must be published before a Connection Hub CLI or KDCube
release that declares its calendar-version floor. `service-foundation` has no
dependency on App Foundation and can be released independently; publish it
before a Problem Board package or procedure relies on its recorded floor.
KDCube maintainer builds can verify coordinated unpublished candidates by
preinstalling each selected local distribution before normal requirement
resolution and reapplying the same source afterward. Ordinary package installs
resolve published distributions, so the production release order remains the
same.
