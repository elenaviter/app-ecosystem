# Connection Hub CLI

`connection-hub-cli` selects the KDCube application host, keeps delegated
caller credentials in the operating-system credential store, connects local
MCP clients to Connection Hub, and operates a running KDCube through exact
delegated management permissions.

The installed command is `connection-hub`. A local caller profile contains an
endpoint, a profile name, a keyring reference, and an optional `access_id`.
The delegated bearer is stored separately in macOS Keychain.
Set `CONNECTION_HUB_STATE_DIR` when non-secret CLI metadata needs an isolated
root; keep the logged-in user's real `HOME` so the operating-system credential
store remains discoverable.

```bash
uv tool install connection-hub-cli
connection-hub setup
connection-hub host authorize
connection-hub host inspect
connection-hub profile add coding-agent
connection-hub client install claude-code --profile coding-agent
```

`setup` creates a dedicated local KDCube runtime by default. It can instead
select an existing local workdir or an existing KDCube endpoint addressed by
loopback, IP, or DNS. New local setup uses Google login by default, asks for
the public Google OAuth Web client ID, and accepts `--auth simple` as an
explicit development option. Endpoint mode currently resolves, probes, and
opens the deployed Connection Hub application.

`connection-hub host authorize` uses the identity provider configured by the
selected KDCube, Authorization Code with PKCE, and an ephemeral loopback
callback. Before contacting KDCube, it verifies a disposable write/read/remove
round trip through the local OAuth credential store. Use
`connection-hub host authorize --no-open` when the CLI cannot launch a browser.
It prints the short-lived authorization URL and waits for the same loopback
callback. The callback page confirms only that the authorization response was
received; the terminal confirms token exchange and Keychain storage.
Its default request contains deployment inspection and application-surface
discovery. The resulting access and refresh credentials remain together in
macOS Keychain. Application reload is requested when it is needed:

```bash
connection-hub host surfaces connection-hub@1-0
connection-hub host reload connection-hub@1-0
```

A reload without reusable authority returns an exact consent request containing
the operation and its currently missing required claims. In an interactive
terminal the CLI opens it, waits for the user, and retries the unchanged request
with the same invocation ID and digest. KDCube resolves the live delegated card
immediately before the operation. The management service controls a running
deployment; local or infrastructure control starts a stopped deployment.

Disconnecting revokes the OAuth grant and its delegated card before removing
the local Keychain session:

```bash
connection-hub host disconnect
```

The client entry launches this common local MCP helper:

```text
connection-hub mcp serve --profile coding-agent
```

The helper reads the delegated bearer from Keychain, opens the governed
Streamable HTTP connection, and relays MCP over stdio. The bearer is absent
from the client file, command arguments, environment, and normal command
output.

The credential belongs to the intended caller profile. Keychain custody
reduces accidental copying; the live delegated-access card remains the
authority. Connection Hub resolves the current card, catalog, expiry,
invocation policy, and revocation on each covered call.

Python host processes can use the same profile boundary without launching the
stdio bridge:

```python
from connection_hub_cli.profile_connection import connect_profile_tools

async with connect_profile_tools(
    profile_name="coding-agent",
    profiles=profiles,
    credentials=credentials,
) as (remote_tools, _mcp_client):
    tools = await remote_tools.list_tools()
```

`connection-hub-cli` owns profile lookup and operating-system credential
custody. The host-neutral MCP session underneath is provided by
[`app-foundation`](https://github.com/elenaviter/app-ecosystem/tree/main/packages/app-foundation).
The application using `remote_tools` still owns its domain calls and result
interpretation.

The same helper can be registered for Claude Code, Claude Desktop local MCP,
Hermes, or OpenClaw:

```text
connection-hub client install <client> --profile coding-agent
```

For another stdio-capable client,
`connection-hub client command --profile coding-agent` prints the non-secret
command and arguments. `connection-hub doctor --probe` verifies local file
permissions, a temporary Keychain write/read/delete, managed client entries,
and MCP tool listing without invoking a tool.

Removing a local profile removes local metadata and Keychain custody. It does
not revoke the server-side delegated card. Edit or revoke that authority from
Connection Hub's authenticated owner surface. Credential replacement applies
to newly started helpers; reload the MCP client after replacement. Removing
client wiring or local custody does not terminate a helper process that is
already running, so revoke the delegated card when access must end
immediately.

This first implementation verifies macOS Keychain. Windows Credential Manager
and Linux Secret Service remain subsequent platform work. KDCube lifecycle and
application-host setup are composed through KDCube's supported control API;
the CLI does not duplicate or shell out to KDCube deployment logic.

Full setup and security boundaries:
https://github.com/elenaviter/app-ecosystem/blob/main/docs/connection-hub/local-client-helper.md

License: MIT. Source: https://github.com/elenaviter/app-ecosystem
