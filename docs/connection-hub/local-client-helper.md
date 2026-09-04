---
id: connection-hub/local-client-helper
title: Connect Desktop MCP Clients To Connection Hub
summary: Choose native remote OAuth or a local stdio bridge with macOS Keychain, Windows Credential Manager, or Linux Secret Service custody.
status: alpha
tags: [connection-hub, mcp, oauth, native-credentials, desktop-clients]
keywords: [connection-hub-cli, delegated caller profile, stdio relay, macOS Keychain, Windows Credential Manager, Linux Secret Service, Claude Code, Codex, Hermes, OpenClaw]
see_also:
  - ./quick-start-local.md
  - ./macos-user-presence-helper.md
  - ./testing/end-to-end-acceptance.md
  - ./package/delegated-cards.md
---

# Connect Desktop MCP Clients To Connection Hub

`connection-hub-cli` connects a desktop MCP client to a governed Connection
Hub MCP endpoint through either of these paths:

```text
native OAuth

MCP client -> remote Streamable HTTP endpoint -> Connection Hub
     |
     +-> browser OAuth + PKCE
     +-> access and refresh credentials in the client's own OAuth store

local bridge

MCP client -> stdio -> connection-hub mcp serve --profile <name>
                              |
                              +-> native OS credential store
                              +-> remote Streamable HTTP endpoint
                              +-> Connection Hub
```

Connection Hub resolves the live delegated card, expiry, invocation policy,
and current operation ceiling on every covered call. The upstream service
credential remains in Connection Hub's server-side custody in both paths.

The separately signed
[macOS user-presence helper](macos-user-presence-helper.md) protects a smaller
KDCube management boundary and follows its own release process.

## Choose A Mode

`client install` accepts `auto`, `oauth`, and `bridge`:

| Mode | Input | Credential owner | Selection rule |
| --- | --- | --- | --- |
| `auto` | `--endpoint` | MCP client | Uses native remote OAuth after verifying the installed client's commands. |
| `auto` | `--profile` | Native OS store | A profile explicitly selects local custody and the stdio bridge. |
| `auto` | `--endpoint` and `--profile` | MCP client, with bridge fallback | Prefers verified native OAuth and uses the supplied profile only when that client's native OAuth commands are unavailable. Both inputs must name the same endpoint. |
| `oauth` | `--endpoint` | MCP client | Requires native remote OAuth and fails before writing configuration when the installed client does not support it. |
| `bridge` | `--profile` | Native OS store | Requires a local static-bearer or OAuth-backed caller profile. |

Stored installation records contain the resolved `oauth` or `bridge` mode.
`auto` is evaluated at installation time. Existing records created before
mode support load as `bridge` records.

## Native Remote OAuth

For a public HTTPS endpoint, install a direct connection:

```bash
connection-hub client install claude-code \
  --mode auto \
  --endpoint https://runtime.example/mcp
```

The result includes a non-secret `authorization_command`. Run that command in
the normal interactive terminal for the client, for example:

```bash
claude mcp login connection-hub-claude-code
```

No local Connection Hub profile is created. OAuth discovery, browser login,
token refresh, and token storage belong to the MCP client. Removing a managed
OAuth installation logs the client out before removing its server entry.

The adapters use these client-owned configuration paths:

| Client | Native OAuth entry | Local bridge entry | Current adapter boundary |
| --- | --- | --- | --- |
| [Claude Code](https://code.claude.com/docs/en/mcp) | `claude mcp add --transport http`, then `claude mcp login` | `claude mcp add --transport stdio` | CLI-managed user-scope entry. |
| [Codex](https://developers.openai.com/codex/mcp) | `codex mcp add --url`, then `codex mcp login --oauth-client-registration auto` | `codex mcp add -- <command>` | CLI-managed entry inspected through `codex mcp get --json`. |
| [Hermes](https://hermes-agent.nousresearch.com/docs/reference/mcp-config-reference) | URL plus `auth: oauth`, then `hermes mcp login` | command plus args | CLI-managed `mcp_servers` entry; direct OAuth also requires an installed version with managed logout. |
| [OpenClaw](https://github.com/openclaw/openclaw/blob/main/docs/cli/mcp.md) | `streamable-http` plus `auth: oauth`, then `openclaw mcp login` | command plus args | CLI-managed `mcp.servers` entry. |
| Claude Desktop | Remote custom connector in Settings | local `mcpServers` entry | The CLI configures the local bridge. Remote OAuth setup remains in the Desktop UI. |

Claude Code `2.1.259` and Codex `0.152.1` were inspected locally while this
contract was implemented. Hermes and OpenClaw adapters follow their linked
primary command/configuration contracts and require real installed-version
acceptance before release claims are made for those clients.

Every command-backed adapter checks its installed help contract before it
writes an entry. The check covers the add, login, logout, inspection, and
removal operations used by that mode. An installed version with an incomplete
OAuth lifecycle is rejected before configuration changes. The adapter
preserves unrelated configuration and verifies only the entry identified by
the managed client/server-name pair.

## Local Bridge Profiles

The bridge keeps caller credentials outside client configuration, process
arguments, environment variables, command history, and ordinary logs. It
supports two profile types.

### Browser-Authorized Profile

Create an OAuth-backed bridge profile through the MCP endpoint:

```bash
connection-hub profile authorize coding-agent \
  --endpoint https://runtime.example/mcp
```

The command:

1. sends an unauthenticated MCP initialize request and reads the protected
   resource challenge;
2. validates protected-resource and authorization-server metadata;
3. selects a provisioned client, Client ID Metadata Document (CIMD), or
   Dynamic Client Registration (DCR);
4. completes browser Authorization Code + PKCE on loopback;
5. probes the governed MCP endpoint;
6. stores the complete token set in the native operating-system store;
7. commits only non-secret profile metadata after storage succeeds.

Use a provisioned public client when the server does not permit dynamic
registration:

```bash
connection-hub profile authorize coding-agent \
  --endpoint https://runtime.example/mcp \
  --client-id provisioned-public-client
```

A CIMD client publishes exact loopback redirect URIs. Select one published
port explicitly:

```bash
connection-hub profile authorize coding-agent \
  --endpoint https://runtime.example/mcp \
  --client-metadata-url https://client.example/oauth/metadata.json \
  --callback-port 9124
```

The command rejects CIMD before browser launch when the server advertises
CIMD and no fixed callback port was supplied.

### Manually Issued Profile

For an existing short-lived delegated caller bearer:

```bash
connection-hub profile add coding-agent \
  --endpoint https://runtime.example/mcp \
  --access-id access_example
```

The hidden prompt accepts the bearer. The CLI probes the endpoint before it
stores the credential and profile metadata. `--credential-stdin` is available
for controlled automation whose standard input already comes from a secret
store.

### Install The Bridge

```bash
connection-hub client install claude-code \
  --mode bridge \
  --profile coding-agent

connection-hub client install codex \
  --mode bridge \
  --profile coding-agent
```

Claude Desktop, Hermes, and OpenClaw accept the same
`--mode bridge --profile` contract. Another stdio-capable client can use the
fragment from:

```bash
connection-hub client command --profile coding-agent
```

The MCP client starts one helper child process and communicates with it over
stdio. The helper opens the remote Streamable HTTP session. A local listening
port and resident relay daemon are unnecessary.

Claude Desktop's local bridge entry is written to its per-user configuration:

| Platform | Local configuration |
| --- | --- |
| macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Windows | `%APPDATA%\Claude\claude_desktop_config.json` |
| Linux | `$XDG_CONFIG_HOME/Claude/claude_desktop_config.json`, or `~/.config/Claude/claude_desktop_config.json` |

`connect_profile_tools()` is the reusable Python composition API for a domain
process that consumes the same profile directly. It resolves and refreshes an
OAuth-backed profile through the same session service used by `mcp serve`.
Callers receive connected MCP tools and never receive refresh credentials.

## Native Credential Stores

The CLI accepts one reviewed keyring backend on each desktop platform:

| Platform | Required store | Accepted backend |
| --- | --- | --- |
| macOS | login Keychain | `keyring.backends.macOS.Keyring` |
| Windows | Credential Manager generic credentials | `keyring.backends.Windows.WinVaultKeyring` |
| Linux desktop | freedesktop Secret Service | `keyring.backends.SecretService.Keyring` |

Null, fail, chainer, plaintext-file, encrypted-file, third-party, and
wrong-platform backends are rejected. There is no plaintext fallback.

Windows generic credentials have a small native value limit. The shared
`app-foundation` store uses a bounded versioned manifest and random generation
of base64 chunks. It writes and verifies all candidate chunks before switching
the manifest, verifies byte count and SHA-256 on read, rolls the manifest back
after a failed commit verification, and cleans the previous generation after
success. Existing direct values remain readable until their next successful
replacement. The logical value bound is 288 KiB of UTF-8 text, enough for the
largest OAuth token record accepted by the CLI after worst-case JSON escaping.

Linux requires a graphical session D-Bus, an available Secret Service
provider, and an unlocked default collection. Headless Linux and WSL without
Secret Service can use a client's native remote OAuth path.

The same selected native backend holds three separate Connection Hub service
names:

```text
delegated static profiles  tech.kdcube.connection-hub.delegated-caller
OAuth bridge profiles      tech.kdcube.connection-hub.oauth-profile
CLI management sessions    tech.kdcube.connection-hub.oauth-session
```

## Refresh, Status, And Recovery

The bridge checks an OAuth token before each new remote connection. Expiring
tokens refresh under the profile transaction lock. Access and refresh token
rotation replaces one complete native-store value; the profile keeps the same
`access_id`. A refresh failure preserves both the profile metadata and prior
token record so the matching server card can still be identified and revoked.

These commands report platform, backend, profile type, endpoint, `access_id`,
credential presence, expiry state, refresh readiness, client mode, and entry
ownership without printing credentials:

```bash
connection-hub status
connection-hub doctor --json
connection-hub doctor --probe --json
connection-hub profile status coding-agent --probe
connection-hub client list --json
```

`doctor` performs a random disposable write/read/delete check in the native
store. On Linux it gives the session D-Bus and Secret Service recovery
requirements. On Windows it names Credential Manager. On macOS it names the
logged-in desktop session and login Keychain.

For a static profile, validate a replacement before switching custody:

```bash
connection-hub profile credential replace coding-agent
```

For an OAuth profile, server revocation precedes local retirement:

```bash
connection-hub profile disconnect coding-agent
```

When local OAuth custody is missing, revoke the recorded `access_id` in the
Connection Hub owner interface first. Local abandonment then requires both an
explicit assertion and the exact identity:

```bash
connection-hub profile remove coding-agent \
  --server-card-revoked \
  --access-id access_example
```

Static profile removal affects local metadata and native-store custody. The
server card is managed separately through the authenticated owner interface.

## State And Trust Boundary

Non-secret state uses the operating system's user configuration directory.
`profiles.json` contains endpoint, profile type, opaque credential reference,
optional `access_id`, and public OAuth metadata. `client-installations.json`
contains the resolved mode and the exact owned entry shape. On macOS and Linux,
the CLI enforces private POSIX modes. On Windows, the files inherit the current
user profile's ACLs and `doctor` does not apply POSIX-bit checks. Credential
values remain in the native store on every platform.

Set a separate metadata root for tests or parallel installations:

```bash
export CONNECTION_HUB_STATE_DIR="$HOME/.connection-hub-test-state"
```

Native per-user storage reduces accidental disclosure. Software running as
the same desktop user may be able to invoke the relay or the operating-system
credential API. Each delegated caller remains short-lived where possible,
independently revocable, and bounded by its live Connection Hub card. This
bridge is not a same-user malware boundary.

Real Windows Credential Manager and Linux Secret Service acceptance remains a
release gate. Mocked backend tests establish portable logic and failure
atomicity; they do not substitute for those desktop runs. See
[End-To-End Acceptance](testing/end-to-end-acceptance.md).
