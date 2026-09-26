# app-foundation

Host-neutral foundations shared by applications in an ecosystem.

## Current Status

`2026.09.26.2220` is released together with service-foundation, connection-hub and
project-board at one version (W322). It contains two
host-neutral foundations: MCP client construction and strict native
credential-value storage.

```bash
python -m pip install 'app-foundation[mcp]'
python -m pip install 'app-foundation[native-secrets]'
python -m pip install 'app-foundation[data-bus]'
```

The package API is under `app_foundation.mcp`:

- `open_mcp_client(...)` opens stdio, SSE, or Streamable HTTP transports;
- `mcp_tool_schema(...)` and `normalize_mcp_tool_result(...)` provide stable
  Python values at the protocol boundary;
- `connect_remote_tools(...)` and `probe_remote_tools(...)` provide a
  Streamable HTTP convenience surface for callers that already possess an
  endpoint and bearer.

Remote connection failures use stable, secret-safe codes. An HTTP `401` or
`403`, including one wrapped by the MCP transport's exception group, becomes
`mcp_authorization_rejected`; response bodies do not cross the boundary.
Timeouts and other connection failures retain their separate codes so a host
can retry connectivity without treating an outage as a credential verdict.

The caller remains responsible for credential custody, authority decisions,
and product-specific error language. Supplying a bearer to the transport does
not grant or evaluate authority.

The native value-store API is under `app_foundation.secrets`:

- `NativeSecretValueStore(...)` selects one reviewed operating-system backend;
- `replace(...)`, `get(...)`, and `remove(...)` manage bounded text values;
- `verify_ready()` proves a disposable write, read, and removal;
- `NativeSecretError` exposes fixed, secret-safe error codes and messages.

The accepted backends are macOS Keychain
(`keyring.backends.macOS.Keyring`), Windows Credential Manager
(`keyring.backends.Windows.WinVaultKeyring`), and Linux Secret Service
(`keyring.backends.SecretService.Keyring`). Null, fail, chainer, file, and
wrong-platform backends are rejected. Windows values use a versioned,
integrity-checked chunk manifest so replacement either selects one complete
new generation or leaves the previous generation readable. The shared bound
is 288 KiB of UTF-8 text, which covers the largest OAuth record accepted by
the Connection Hub CLI after JSON escaping.

The shared store knows only service names, account keys, and text values. A
consuming product owns serialization, logical namespaces, access policy,
recovery, and the meaning of each secret.

The Data Bus client API is under `app_foundation.data_bus`:

- `DataBusClaim.from_mapping(...)` validates a short-lived, bundle-scoped
  claim without rendering its token;
- `FederatedDataBusClient.connect()` opens the authenticated Socket.IO lane;
- `close()` stops either a connected socket or an in-progress reconnect loop
  and waits until the transport task has ended;
- `request(...)` distinguishes ingress acceptance from the handler's
  correlated terminal result;
- `wait_for_event(...)` receives application push events that are not replies
  to an in-flight request.

The client constructs transport envelopes and correlates replies. The
application owns subjects, operation names, domain authorization, and the
meaning of pushed events. It refuses an expired claim before connection and
bounds event waits by claim expiry. A Data Bus receipt broadcast by the
session for another peer is ignored by this client; it cannot wake an
application event loop. An ingress acknowledgement timeout leaves
acceptance unknown; a terminal result received before that timeout is returned.
An accepted request without a terminal result is also outcome unknown. In
either unknown case, the product preserves the original message and operation
identity for a retry. Client errors include the logical connection generation,
Socket.IO connection ID, and connection state captured when the request began.
The client logs successful connections, disconnects, reconnects, and refused
connection attempts with the same generation and connection ID. A refused
attempt explicitly records whether it replaced the last successful generation,
so a product can correlate one request without treating reconnect activity as
proof that its connection changed. A product that replaces client objects may
provide non-secret `lifecycle_labels`; those labels appear on every lifecycle
record and let the product carry its stable channel identity and replacement
epoch across object-local generation resets.

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
