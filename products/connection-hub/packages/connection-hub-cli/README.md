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

## Manage And Export Secrets

An authorized operator or automation can manage one exact secret through its
live Connection Hub Card:

```bash
connection-hub host secret metadata services.brave.api_key --scope platform
connection-hub host secret set services.brave.api_key --scope platform
connection-hub host secret get services.brave.api_key --scope platform \
  --output ./brave-api-key
connection-hub host secret delete services.brave.api_key --scope platform
```

`set` uses a hidden prompt by default. `get` writes a `0600` local file on
POSIX, uses the selected parent directory's ACL on Windows, and reports
disclosure metadata without printing the value. Connection Hub checks the
exact target and operation on every call; its Card editor supplies `Once` and
`Always` invocation policy. Before requesting disclosure, `get` rejects a
missing parent, an existing destination without `--replace`, or a non-file
replacement target. The atomic writer repeats these checks when publishing so
a filesystem race cannot silently clobber another path.

Descriptor export is an owner-performed path with independent authority:

```bash
connection-hub host secret export \
  --platform-key services.brave.api_key \
  --bundle-key connection-hub@1-0=connections.oauth_state_secret \
  --output-directory ./kdcube-secret-export-20260904
```

The command starts a PKCE-bound loopback callback and opens the KDCube approval
page. When no platform browser session exists, KDCube redirects through the
identity provider configured by that deployment. The signed-in platform
administrator sees the exact deployment, callback, digest, and key list, then
chooses `Export once`. One authorization code permits one exchange for that
manifest. The flow leaves delegated Cards unchanged and stores no reusable
export credential.

The destination must be a new directory. The CLI validates a bounded response,
stages and flushes canonical `secrets.yaml` and `bundles.secrets.yaml`, then
atomically reserves the destination without replacing any existing path. It
moves only complete files into that owned directory and returns success after
both are durable; a handled failure removes its partial destination. POSIX
permissions are `0700` for the directory and `0600` for each file. Windows
inherits the ACL of the selected parent directory, so that parent must already
be private to the intended Windows user. The CLI prints only paths, counts,
request digest, and assurance evidence.

Key names are explicit because every supported provider can resolve an exact
key while some secure providers intentionally cannot enumerate original key
names. Repeat `--platform-key` and `--bundle-key BUNDLE_ID=KEY` for the desired
manifest. The same command works with local and remote KDCube hosts and with
the file, host-vault, and cloud secret backends.

The built-in `session_confirmation` assurance proves a current KDCube admin
browser session plus the exact click. Deployments configured for
`fresh_authentication` or `user_verification` fail closed until their
configured identity authority installs a verifier that can redirect through a
fresh IdP or WebAuthn/passkey challenge and return evidence bound to the exact
request digest and verification time. The transaction and CLI protocol remain
the same for those stronger verifiers.

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
