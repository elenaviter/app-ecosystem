# service-foundation

Foundations for standalone runnable services and host-side companion
processes.

## Current Status

`2026.09.03.1835` is the first implementation candidate. It includes a generic
host-relay lifecycle under `service_foundation.host_relay`:

- `HostRelayAdapter` defines one asynchronous `poll_once()` domain boundary;
- `HostRelayRuntime` owns repeated execution, health, stop, and bounded retry;
- `HostRelayPolicy` defines poll and retry timing;
- entry-point discovery uses `service_foundation.host_relay.adapters` for
  separately packaged adapters.

```bash
python -m pip install service-foundation
```

An adapter owns domain translation and returns bounded cycle metadata. The
runtime emits lifecycle metadata and result-key names; it does not copy domain
payloads into observer events.

## Boundary

A standalone service needs a host layer around its application logic:

- composition and launcher contracts;
- browser-session and service-workload authentication surfaces;
- service-owned session issuance and verification;
- configuration, health, and readiness contracts;
- migration invocation, while the application owns its migrations;
- lifecycle for a local or remote companion process.

The host-relay runtime does not know MCP, credentials, agents, mail, journals,
or any product vocabulary. A product composition root supplies an adapter and
opens whatever governed transport that adapter needs.

`service-foundation` does not import `app-foundation`, and `app-foundation`
does not import `service-foundation`. Products may depend on either or both.

License: MIT. Source: https://github.com/elenaviter/app-ecosystem
