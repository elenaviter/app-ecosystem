# app-foundation

Host-neutral foundations shared by applications in an ecosystem.

## Current Status

`2026.10.03.0811` is released together with service-foundation, connection-hub and
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
bounds event waits by claim expiry. Namespace admission has a configurable
30-second default, on both the initial connection and App Foundation-owned
reconnect attempts, so server-side Card verification and delivery of its
completion packet are not cut off by the Socket.IO client's one-second default
or by a busy client event loop at the former 15-second boundary. On deadline
the client retires and closes that transport once. Each App Foundation-owned
attempt has a distinct transport identity, so an acceptance or refusal queued
by an earlier attempt cannot complete a later attempt. This connection-only
bound does not change ingress or correlated-outcome timeouts. A Data Bus receipt
broadcast by the session for another peer is ignored by this client; it cannot
wake an application event loop. An ingress acknowledgement timeout leaves
acceptance unknown; a terminal result received before that timeout is returned.
When the timed-out transport is still the active App Foundation-owned one,
delivered nothing since the request was sent, and the timer fired on time, the
client treats it as silent and drops it through the normal disconnect path, so
the reconnect starts at once instead of when Engine.IO gives up. The client
keeps the old transport until its resources are released: a bounded graceful
disconnect, then an abort that closes its WebSocket and HTTP session, also when
the disconnect hangs or the client is closed. Requests already waiting for
their outcome keep waiting and resolve on the reconnected session. `transport_recovering` tells an owner that a drop is only transport:
the client is reconnecting on its own, within a bounded window, with no
handshake refused in the episode and an unexpired credential. Each reconnect
handshake logs the time since the drop, the transport open time and the
credential source time.
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

A failed App Foundation-owned reconnect attempt is retried by default, with a
doubling delay up to a cap, until the client is closed. An owner that knows
which refusals cannot heal by retrying passes two optional constructor
arguments:

- `refusal_classifier(error) -> bool` is called with each failed reconnect
  attempt's exception and returns True when the refusal is permanent (for
  example, a revoked credential). The client knows no product codes; the owner
  supplies them all.
- `on_terminal_refusal(record)` is called once, when the classifier first
  answers True, with the same record `terminal_refusal` returns.

Both callbacks are synchronous: they run on the client's event loop, must not
block, and must not await. A classifier that raises is logged and its answer
is taken as transient, so a fault in the owner's code never ends the
reconnect. An exception from `on_terminal_refusal` is logged and does not
change the terminal state.

After a permanent refusal the client is terminal. `terminal_refusal` returns
`state` `refused_permanent` with the error code and type,
`transport_recovering` is False, and the reconnect loop has stopped. A
terminal client stays disconnected until it is closed: `connect()` raises
`DataBusClientError` with the stored code and the `terminal_refusal` record
in its details, without an attempt, and no disconnect or reconnect path opens
it again. There is no reset. The owner closes the client and creates a new
one, for example with a new credential. Without a classifier, behaviour is
unchanged: every failure is retried until close.

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
