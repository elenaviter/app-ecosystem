---
id: connection-hub/local-client-helper
title: Connect Local MCP Clients Without Bearer Files
summary: Keep one delegated caller credential in macOS Keychain and install the same local Connection Hub MCP helper into Claude Code, Claude Desktop, Hermes, OpenClaw, or another stdio client.
status: alpha
tags: [connection-hub, mcp, keychain, claude-code, claude-desktop, hermes, openclaw]
keywords: [connection-hub-cli, delegated caller profile, local MCP helper, macOS Keychain, stdio relay]
see_also:
  - ./quick-start-local.md
  - ./macos-user-presence-helper.md
  - ./testing/end-to-end-acceptance.md
  - ./package/delegated-cards.md
---

# Connect Local MCP Clients Without Bearer Files

`connection-hub-cli` keeps each manually issued delegated caller credential in
macOS Keychain and installs one local stdio MCP helper into supported clients.
The client configuration contains the helper command and caller-profile name;
it does not contain the bearer.

This page describes the Python MCP relay and its ordinary Keychain custody.
The separately signed Rust helper for user-presence-protected KDCube management
has a different operation boundary and release path. See
[Protect KDCube Management On macOS With User Presence](macos-user-presence-helper.md).

The helper opens Connection Hub's governed Streamable HTTP endpoint when the
client starts it. It forwards current tools, calls, structured denials,
cancellation, progress, and tool-list changes. Connection Hub still resolves
the live delegated card, expiry, `Once` or `Always` invocation policy, and
revocation on every covered call.

## Before Connecting A Client

Select and open the Connection Hub application host:

```bash
connection-hub setup
```

The default creates a dedicated local KDCube runtime with Google login and asks
for its public OAuth Web client ID. Use `--auth simple` only for an explicit
local-development login. Use `--local-workdir` for an existing local runtime,
or `--endpoint` with tenant and project coordinates for an already deployed
endpoint. See the [local KDCube quick start](quick-start-local.md) for those
forms and the delegated remote-management design. In the browser:

1. Connect the external MCP service or another protected service.
2. Create a separate delegated caller profile for the client.
3. Select its exact resources, tools, operations, claims, and invocation
   policy.
4. Copy the governed `remote_mcp_proxy` endpoint and the one-time-shown caller
   bearer.

Use a separate server-side card and local profile when two clients need
independent authority, revocation, or audit identity. Reusing one profile in
several clients deliberately gives them the same `access_id` and authority.

## Install The Helper

For a source checkout:

```bash
python3 -m pip install \
  ./products/connection-hub/packages/connection-hub-cli
```

After a package release, install it as an isolated command:

```bash
uv tool install connection-hub-cli
```

The installed distribution is `connection-hub-cli`; its command is
`connection-hub`.

## Store One Caller Profile

Run:

```bash
connection-hub profile add coding-agent \
  --access-id access_example
```

The command asks for the bearer through hidden terminal input. It first opens
the MCP connection and lists tools. Only after that probe succeeds does it put
the credential in Keychain and commit the non-secret profile metadata.

The endpoint defaults to the governed MCP surface of the selected application
host. `--endpoint` remains available when a caller profile intentionally uses a
different Connection Hub endpoint.

The optional `--access-id` is metadata for identifying the matching card. It
is not used as authorization. Do not pass the bearer as an argument or put it
in the endpoint URL. `--credential-stdin` exists for controlled automation
whose standard input already comes from a secret store.

## Install The Client Entry

Each adapter registers the same helper command and verifies what the client
retained:

```bash
connection-hub client install claude-code --profile coding-agent
connection-hub client install claude-desktop --profile coding-agent
connection-hub client install hermes --profile coding-agent
connection-hub client install openclaw --profile coding-agent
```

The adapters use each client's own configuration surface:

| Client | Registration path |
| --- | --- |
| [Claude Code](https://code.claude.com/docs/en/mcp) | `claude mcp` user-scope commands |
| [Claude Desktop](https://support.claude.com/en/articles/10949351-getting-started-with-local-mcp-servers-on-claude-desktop) | local `mcpServers` configuration |
| [Hermes](https://hermes-agent.nousresearch.com/docs/reference/mcp-config-reference) | `hermes mcp` commands and `mcp_servers` registry |
| [OpenClaw](https://github.com/openclaw/openclaw/blob/main/docs/cli/mcp.md) | `openclaw mcp` commands and `mcp.servers` registry |

OpenClaw versions and runtime adapters differ in which configured MCP registry
is visible to the active agent. Run its own MCP status or probe command after
installation and confirm that the intended runtime lists the Connection Hub
tools.

For another stdio-capable client, print the non-secret command and arguments:

```bash
connection-hub client command --profile coding-agent
```

The resulting entry is equivalent to:

```text
connection-hub mcp serve --profile coding-agent
```

No local relay daemon or listening port is created. The MCP client starts the
helper as a child process and communicates with it over stdio.

## Inspect And Repair Local State

Connection Hub CLI stores non-secret metadata in the operating system's user
configuration directory. Tests or parallel installations can select another
root without changing the operating-system account environment:

```bash
export CONNECTION_HUB_STATE_DIR="$HOME/.connection-hub-test-state"
```

The directory contains profile, host, client-installation, and OAuth-session
coordinates. Credentials remain in the operating-system credential store. Do
not override `HOME` merely to move this metadata: on macOS, doing so can leave
the process without the user's default Keychain.

These commands never print the bearer:

```bash
connection-hub status
connection-hub doctor
connection-hub doctor --probe
connection-hub profile status coding-agent --probe
```

`doctor` checks private state-file modes, performs a temporary Keychain
write/read/delete, checks profile credential records and managed client
entries, confirms their helper executable still exists, and optionally checks
MCP reachability. A probe initializes MCP and lists tools; it does not invoke a
tool.

Replace a reissued caller credential only after validating it:

```bash
connection-hub profile credential replace coding-agent
```

An already running helper keeps the credential it loaded when that process
started. Reload or restart the MCP client after replacement so it launches a
new helper with the replacement credential. The command reports this restart
requirement in its result.

Remove client wiring before removing the local profile:

```bash
connection-hub client remove claude-code connection-hub-coding-agent
connection-hub profile remove coding-agent
```

Local removal deletes the local profile and its Keychain item. It does not
revoke the delegated card in Connection Hub. Revoke or edit that card through
the authenticated Connection Hub owner surface. Removing a client entry or
local profile also does not terminate a helper process that is already
running. Reload the client, and revoke the delegated card when access must end
immediately.

## Authorize This CLI To Manage A Running KDCube

The client profiles above authorize MCP clients. A separate OAuth-managed
caller profile authorizes this CLI to inspect and operate the selected KDCube:

```bash
connection-hub host authorize
connection-hub host inspect
connection-hub host surfaces connection-hub@1-0
connection-hub host reload connection-hub@1-0
```

Use `connection-hub host authorize --no-open` when the CLI cannot launch a
browser. The command prints the short-lived authorization URL and waits on its
ephemeral loopback callback.

Authorization first proves a disposable random write/read/remove round trip
through the OAuth credential store. Failure stops before OAuth discovery, DCR,
browser login, or card creation. The browser callback confirms receipt of the
authorization response; the terminal confirms token exchange and Keychain
storage.

The selected KDCube owns the browser login and uses its configured identity
provider. The default authorization asks for deployment inspection and public
application-surface discovery. Reload remains demand-driven. Its first denied
call can open a consent page containing the operation and its currently missing
required claims, bound to the exact application, invocation ID, and request
digest, then retry that same request after approval.

The OAuth access and refresh credentials are stored as one macOS Keychain
item. The non-secret `oauth-sessions.json` record contains the selected target,
issuer, resource, public client ID, Keychain reference, and timestamps.
`connection-hub status` and `connection-hub doctor` verify the corresponding
Keychain item without printing either credential.

Use `connection-hub host disconnect` to revoke the server-side OAuth grant and
delegated card before removing the local session. The command retains local
state when server revocation fails so the user can retry safely.

## Credential Boundary

Keychain custody prevents routine copying into MCP configuration, environment
variables, command history, and logs. It does not make the credential
unavailable to software running as the same operating-system user. The
intended MCP client starts the helper, and the helper uses that profile's
bounded authority.

`profiles.json` contains the profile's random `credential_ref` and optional
`access_id`. Those values are lookup coordinates, not secrets. The current
macOS backend stores an ordinary generic-password item and does not install a
CLI-binary restriction, mandatory user-presence prompt, or device-bound
proof. A process running as another OS user cannot use the owner's login
Keychain merely by reading the profile file. A process running as the same
logged-in user may be able to request the item under the user's Keychain
policy, and may also invoke the configured helper directly. Use this path for
a caller that is intentionally trusted to exercise the card's bounded
authority.

The pre-release macOS presence helper supplies the stronger local interaction
for delegated KDCube management. It is a separately signed Rust application
with a provisioned Keychain access group. It owns the complete access/refresh
OAuth session and executes one registered operation after macOS user presence;
credential bytes do not cross into Python. It remains unavailable through the
shared CLI until its signing, notarization, real-prompt acceptance, and command
integration gates pass. Its complete contract and user procedure are in
[Protect KDCube Management On macOS With User Presence](macos-user-presence-helper.md).

Other environments use their native interaction or a request-bound browser
flow: Windows Hello on Windows, Secret Service plus polkit or PAM on Linux, and
browser or device approval where no local user session exists. A container
calls a narrowly scoped helper on its host; it does not receive a mounted
credential store.

The upstream service credential remains inside Connection Hub. The local
helper receives only the delegated caller credential. It accepts HTTPS
endpoints, plus plain HTTP on loopback for local development, and does not
forward the bearer across redirects.

Clients with a usable remote-MCP OAuth flow can connect directly to a publicly
reachable Connection Hub endpoint. In that flow, the client's OAuth store owns
its access and refresh credentials, and no manual Keychain profile is needed.

The current helper verifies macOS Keychain. Windows Credential Manager and
Linux Secret Service require separate platform verification. Connection Hub
host setup composes KDCube's supported target-control library. Local targets
support initialization and lifecycle. Local and endpoint targets support
delegated inspection, surface discovery, and exact application reload while
they are running. Each management request carries the OAuth credential for the
CLI caller profile, and KDCube resolves that profile's live card before the
operation.
