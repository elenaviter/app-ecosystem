# Connection Hub CLI

`connection-hub-cli` keeps one delegated caller credential in the operating
system credential store, selects the KDCube application host, and connects
local MCP clients to a Connection Hub Streamable HTTP endpoint without copying
that credential into every client configuration.

The installed command is `connection-hub`. A local caller profile contains an
endpoint, a profile name, a keyring reference, and an optional `access_id`.
The delegated bearer is stored separately in macOS Keychain.

```bash
uv tool install connection-hub-cli
connection-hub setup
connection-hub profile add coding-agent
connection-hub client install claude-code --profile coding-agent
```

`setup` creates a dedicated local KDCube runtime by default. It can instead
select an existing local workdir or an existing KDCube endpoint addressed by
loopback, IP, or DNS. New local setup uses Google login by default, asks for
the public Google OAuth Web client ID, and accepts `--auth simple` as an
explicit development option. Endpoint mode currently resolves, probes, and
opens the deployed Connection Hub application. A future remote management API
can authorize this CLI through a normal delegated caller card containing exact
KDCube management resources and operations.

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
