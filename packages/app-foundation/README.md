# app-foundation

Host-neutral foundations shared by applications in an ecosystem.

## Current Status

`2026.09.03.1835` is the first implementation candidate. It extracts the
generic MCP client realm that previously lived inside the KDCube monorepo:
transport construction, dual-era protocol negotiation, tool-schema
normalization, result normalization, and an authenticated remote-tools
connection helper.

```bash
python -m pip install 'app-foundation[mcp]'
```

The package API is under `app_foundation.mcp`:

- `open_mcp_client(...)` opens stdio, SSE, or Streamable HTTP transports;
- `mcp_tool_schema(...)` and `normalize_mcp_tool_result(...)` provide stable
  Python values at the protocol boundary;
- `connect_remote_tools(...)` and `probe_remote_tools(...)` provide a
  Streamable HTTP convenience surface for callers that already possess an
  endpoint and bearer.

The caller remains responsible for credential custody, authority decisions,
and product-specific error language. Supplying a bearer to the transport does
not grant or evaluate authority.

## Boundary

Applications serving real users repeatedly need the same host capabilities:

- principal and service-identity contracts;
- secret-reference resolution and vault adapters;
- Postgres and Redis clients, cache, compare-and-set, and distributed locks;
- HTTP, CSRF, and external-URL utilities;
- events and observability primitives;
- protocol clients and neutral result conversion.

`app-foundation` owns reusable application-facing mechanisms. Product
authority, application-domain behavior, standalone process lifecycle, and
deployment orchestration remain outside this package.

`app-foundation` does not import `service-foundation`. The two distributions
can be composed by a product without creating a dependency cycle.

## Extraction Contract

The production implementations being separated live in
[KDCube](https://github.com/kdcube/kdcube). Each extraction moves one verified
contract into this package, then leaves the former KDCube path as a thin
compatibility import. KDCube's MCP agent adapter and distributed server
defaults remain platform-owned; they are not part of this client extraction.

License: MIT. Source: https://github.com/elenaviter/app-ecosystem
