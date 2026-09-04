# Connection Hub CLI

`connection-hub-cli` starts or selects a KDCube-hosted Connection Hub,
authorizes exact management operations, and connects desktop MCP clients to a
governed MCP endpoint. The installed command is `connection-hub`.

```bash
uv tool install connection-hub-cli
connection-hub setup
```

`setup` creates a dedicated local KDCube runtime by default. It can also select
an existing local workdir or an HTTPS KDCube endpoint. New local setup uses
Google login by default and accepts `--auth simple` for an explicit development
environment.

## Connect An MCP Client

Two connection modes cover different credential owners:

| Mode | Remote transport | Credential custody |
| --- | --- | --- |
| `oauth` | Client connects directly by Streamable HTTP | The MCP client's OAuth store |
| `bridge` | Client starts `connection-hub mcp serve` over stdio | The current user's native operating-system store |
| `auto` | Selects a verified native OAuth path, or a supplied local profile | The selected mode's owner |

For Claude Code, Codex, Hermes, or OpenClaw with native remote OAuth:

```bash
connection-hub client install claude-code \
  --mode auto \
  --endpoint https://runtime.example/mcp
```

The JSON result includes the non-secret interactive authorization command.
Run it in the client's normal terminal. Claude Desktop remote connectors are
created in its Settings interface; the CLI manages its local stdio entry.
Direct OAuth does not construct or probe the Connection Hub native store, so
it remains available on headless Linux and WSL installations where the MCP
client has its own secure OAuth custody.

For a local bridge, first create a profile through browser OAuth and PKCE:

```bash
connection-hub profile authorize coding-agent \
  --endpoint https://runtime.example/mcp
connection-hub client install claude-code \
  --mode bridge \
  --profile coding-agent
```

The bridge retrieves and refreshes the OAuth token set internally. The client
configuration contains only this command shape:

```text
connection-hub mcp serve --profile coding-agent
```

A manually issued short-lived delegated bearer can initialize a static bridge
profile through hidden input:

```bash
connection-hub profile add coding-agent \
  --endpoint https://runtime.example/mcp \
  --access-id access_example
```

Use `--credential-stdin` only when standard input already comes from a
controlled secret store. The profile metadata files contain an opaque
credential reference and optional `access_id`; bearer and OAuth token values
remain in native custody.

The accepted native backends are:

| Platform | Store | Exact backend |
| --- | --- | --- |
| macOS | login Keychain | `keyring.backends.macOS.Keyring` |
| Windows | Credential Manager | `keyring.backends.Windows.WinVaultKeyring` |
| Linux desktop | Secret Service | `keyring.backends.SecretService.Keyring` |

Windows values use a bounded, versioned chunk representation with digest
verification and transactional generation replacement. Its 288 KiB UTF-8
bound covers every token record accepted by this CLI, including worst-case
JSON escaping. Linux bridge mode requires a graphical session D-Bus, an
available Secret Service provider, and an unlocked default collection.
Backend selection fails closed and has no plaintext fallback.

Command-backed adapters verify add, login, logout, inspection, and removal
capabilities relevant to the selected mode before writing an entry. A client
version with an incomplete OAuth lifecycle is rejected; `auto` can use an
explicitly supplied bridge profile instead.

## Operate The Selected KDCube Host

Authorize the CLI through the identity provider configured by the selected
KDCube deployment:

```bash
connection-hub host authorize
connection-hub host inspect
connection-hub host surfaces connection-hub@1-0
connection-hub host reload connection-hub@1-0
```

`host authorize` performs native-store readiness first, then OAuth discovery,
Authorization Code with PKCE, browser login, token exchange, and card-bound
session storage. Reload permission is demand-driven and request-bound. The
runtime resolves the live delegated card immediately before the operation.

```bash
connection-hub host disconnect
```

Disconnect revokes the OAuth grant and its delegated card before removing
local custody.

## Diagnose And Recover

```bash
connection-hub status
connection-hub doctor --probe --json
connection-hub profile status coding-agent --probe
connection-hub client list --json
```

Diagnostics report the platform, exact backend, profile type, expiry state,
refresh readiness, client mode, and owned-entry state. Credential values are
excluded from command arguments, environment variables, client configuration,
state files, output, and ordinary logs.

Static credential replacement validates the candidate before switching:

```bash
connection-hub profile credential replace coding-agent
```

OAuth profile disconnection revokes server authority before local retirement:

```bash
connection-hub profile disconnect coding-agent
```

If local OAuth custody is missing, revoke the recorded `access_id` through the
Connection Hub owner interface before using the explicit local-abandonment
flags on `profile remove`.

Set `CONNECTION_HUB_STATE_DIR` when non-secret CLI metadata needs an isolated
root. Keep the real desktop user's `HOME` so its native store remains
discoverable.

## Python Composition

Domain host processes can use the same profile boundary directly:

```python
from connection_hub_cli.profile_connection import connect_profile_tools

async with connect_profile_tools(
    profile_name="coding-agent",
    profiles=profiles,
    credentials=credentials,
    oauth_sessions=oauth_sessions,
) as (remote_tools, _mcp_client):
    tools = await remote_tools.list_tools()
```

`connect_profile_tools()` uses the same protected OAuth profile session as the
stdio relay. The domain caller receives connected tools while refresh
credentials remain inside the profile service.

The complete command, security, client, recovery, and acceptance contracts are
documented in [Connect Desktop MCP Clients To Connection Hub](https://github.com/elenaviter/app-ecosystem/blob/main/docs/connection-hub/local-client-helper.md).
Real Windows Credential Manager, Linux Secret Service, Hermes, and OpenClaw
acceptance is recorded separately from mocked portable tests.

License: MIT. Source: https://github.com/elenaviter/app-ecosystem
