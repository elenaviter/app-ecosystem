---
id: connection-hub/macos-user-presence-helper
title: Protect KDCube Management On macOS With User Presence
summary: Install, verify, operate, update, and remove the signed Rust helper that keeps a delegated KDCube management OAuth session behind macOS user presence.
status: pre-release
updated_at: 2026-09-03
tags: [connection-hub, macos, keychain, user-presence, oauth, kdcube-management]
keywords: [Connection Hub presence helper, Touch ID, macOS Keychain, protected OAuth session, signed helper]
see_also:
  - ./quick-start-local.md
  - ./local-client-helper.md
  - ./package/delegated-cards.md
  - ./testing/end-to-end-acceptance.md
  - https://github.com/kdcube/kdcube/blob/main/app/ai-app/docs/service/cicd/delegated-management-service-README.md
---

# Protect KDCube Management On macOS With User Presence

The Connection Hub macOS presence helper is a separately signed Rust
application for sensitive operations against a running KDCube deployment. It
owns browser authorization, the complete OAuth session, protected Keychain
access, refresh-token rotation, and the exact HTTP execution. The calling
Python process receives bounded operation evidence; it does not receive the
access token or refresh token.

## Release Status

The helper is **pre-release**. Its isolated implementation, automated tests,
artifact verification, atomic installation, upgrade, rollback preservation,
and fail-closed uninstall path exist in source. A supported user release still
requires all of these gates:

- an official Developer ID signature, matching provisioning profile, and
  notarized release artifact;
- successful real macOS authorization, cancellation, execution, and cleanup
  acceptance with the provisioned application;
- a published Team ID, archive checksum, supported CPU architecture, and
  release version;
- a reviewed native-artifact publication workflow or an explicit supervised
  release procedure; the Python package workflow does not publish this
  application;
- integration into an explicitly documented `connection-hub` command;
- clean-account installation, upgrade, and uninstall evidence.

Installing the current source artifact does not make existing
`connection-hub host ...` commands use this helper. Until a release explicitly
names that integration, those commands retain the ordinary Keychain behavior
described in [Connect Local MCP Clients Without Bearer Files](local-client-helper.md).

## Choose The Local Credential Path

| Need | Path | Local boundary |
| --- | --- | --- |
| Give a trusted local MCP client or coding agent its own bounded caller profile | `connection-hub-cli` local MCP relay | The bearer is kept out of client files and stored in ordinary macOS Keychain. Software running as the same logged-in user may still request or exercise it. |
| Keep sensitive KDCube management OAuth credentials inside a narrow executable and confirm each protected use | Signed macOS presence helper | The release helper is signed and entitled for its protected Keychain access group. It executes one compiled operation after macOS user presence and returns secret-free evidence. |
| Approve an operation from a container, headless worker, or another platform | Connection Hub's request-bound browser approval | The protected service validates the exact resource, operation, application, invocation, and digest before applying one-use or reusable authority. |

The two local helpers have different jobs. The Python MCP relay carries the
selected MCP client's normal delegated traffic. The Rust presence helper is a
one-shot management executor; it is not an MCP relay and does not accept an
arbitrary URL, method, body, or caller-provided transport.

## Supported Management Operations

The helper build contains this fixed operation registry:

| Operation | Effect |
| --- | --- |
| `kdcube.management.deployment.inspect` | Read the selected running deployment's bounded management view. |
| `kdcube.management.application.surfaces.read` | Read public surfaces for one validated application identifier. |
| `kdcube.management.application.reload` | Request reload of one validated application with the fixed `{}` body and an invocation identifier. |

KDCube remains the enforcement point. It resolves the current delegated card,
resource and operation grants, catalog and policy revisions, revocation, and
request permit immediately before the operation. macOS user presence confirms
local credential use; it does not expand the card.

## Requirements For Released Users

- macOS 13 or newer on a CPU architecture named by the release;
- a login password or biometric accepted by macOS user-presence policy;
- an interactive logged-in desktop session for browser login and system
  authentication prompts;
- a running HTTPS KDCube endpoint that publishes the matching Connection Hub
  OAuth and delegated-management contracts;
- the official signed and notarized helper archive from the App Ecosystem
  release.

Released users do **not** need Xcode, Rust, an Apple Developer membership, a
signing identity, a Team ID, or a provisioning profile. Those belong to the
release-maintainer path.

## Install A Released Artifact

The release must publish the exact version, expected Apple Team ID, and SHA-256
next to the helper archive. Substitute only values from that release record:

```bash
HELPER_ZIP="$HOME/Downloads/ConnectionHubPresenceHelper-<version>.zip"
EXPECTED_SHA256="<sha256-from-the-release>"
KDCUBE_HELPER_TEAM_ID="<published-10-character-team-id>"

printf '%s  %s\n' "$EXPECTED_SHA256" "$HELPER_ZIP" | shasum -a 256 -c -

HELPER_UNPACK="$(mktemp -d /private/tmp/connection-hub-presence-install.XXXXXX)"
ditto -x -k "$HELPER_ZIP" "$HELPER_UNPACK"
HELPER_APP="$HELPER_UNPACK/ConnectionHubPresenceHelper.app"

"$HELPER_APP/Contents/Resources/verify-app.sh" \
  --app "$HELPER_APP" \
  --expected-team-id "$KDCUBE_HELPER_TEAM_ID"

"$HELPER_APP/Contents/Resources/manage-user-install.sh" install \
  --app "$HELPER_APP" \
  --expected-team-id "$KDCUBE_HELPER_TEAM_ID"
```

Verification checks the exact bundle identifier, release-version shape,
Developer ID Team ID, Keychain access-group entitlement, embedded provisioning
profile, notarization ticket, Gatekeeper assessment, and writable-file modes.
Installation verifies again after copying and atomically points `current` at
the installed version under:

```text
~/Library/Application Support/KDCube/ConnectionHubPresenceHelper/
```

Do not bypass Gatekeeper, remove the quarantine attribute, ad-hoc sign the
application, or substitute a locally rebuilt binary. Those actions change the
identity that protects the Keychain access group.

## Normal Use

The supported `connection-hub` integration will drive the helper through one
JSON request and one JSON response per process. A normal management session is:

```text
connection-hub command
        |
        | target coordinates + registered operation
        v
signed Rust helper
        |
        +-- browser login and delegated-card authorization
        +-- protected access + refresh session in Data Protection Keychain
        +-- short prompt: operation + tenant/project + normalized origin
        +-- exact HTTP request to the selected KDCube
        |
        v
secret-free result or fixed error
```

On each protected operation:

1. Read the operation, application identifier, invocation identifier, target,
   and all bounds in the system prompt.
2. Cancel when the operation or target is unexpected. Cancellation dispatches
   no HTTP operation.
3. Approve through the macOS system prompt when the request is expected.
4. Complete any request-bound KDCube browser approval when the delegated card
   requires `Allow once` or `Allow always` for that exact operation.

An expired access token is refreshed inside the helper under a per-session
lock. Refresh-token rotation is atomically stored before the management call.
When refresh can no longer complete, the helper returns
`session_reauthorization_required` and the user starts browser authorization
again.

## Upgrade

Verify the new release exactly as for installation, then run its installer:

```bash
"$HELPER_APP/Contents/Resources/manage-user-install.sh" upgrade \
  --app "$HELPER_APP" \
  --expected-team-id "$KDCUBE_HELPER_TEAM_ID"
```

The installer rejects a modified, incorrectly signed, unnotarized, or already
installed version. It verifies the current and candidate applications, stages
the new version, verifies the staged copy, and changes the `current` symlink
only after those checks pass. Previous version directories remain available
for inspected rollback handling; replacing `current` manually is not a
supported rollback procedure.

## Disconnect And Uninstall

Use the supported Connection Hub command to disconnect a protected session
before removing software. To remove the helper itself:

```bash
INSTALLED_HELPER="$HOME/Library/Application Support/KDCube/ConnectionHubPresenceHelper/current"

"$INSTALLED_HELPER/Contents/Resources/manage-user-install.sh" uninstall \
  --expected-team-id "$KDCUBE_HELPER_TEAM_ID"
```

Uninstall verifies the installed application, prompts as required, revokes
every helper-owned OAuth session, removes its protected Keychain records, and
then removes the install root. If revocation or protected-record cleanup fails,
the command exits nonzero and leaves the helper installed so cleanup can be
retried.

## Security Boundary

- The helper stores each complete OAuth session in the Data Protection
  Keychain with `AccessibleWhenPasscodeSetThisDeviceOnly` and
  `kSecAccessControlUserPresence`.
- The signed application and its provisioned Keychain access group establish
  credential custody. Python receives session identifiers and bounded results,
  not credential bytes.
- The helper derives the system prompt from validated structured fields. The
  prompt starts with the operation and includes the normalized origin with its
  port plus tenant/project coordinates.
- The HTTP client is constructed inside the helper, disables redirects and
  environment proxies, and accepts HTTPS plus explicit loopback HTTP for local
  development.
- OAuth discovery, authorization, token, refresh, and revocation endpoints are
  validated before use. The access and refresh credentials remain inside the
  helper lifecycle.
- Fixed errors contain no wrapped transport exception, response body, token,
  or credential-bearing cause.
- Mutable buffers owned by the helper are wiped when released. macOS
  frameworks, TLS libraries, and language runtimes can create transient copies
  that the helper cannot guarantee are zeroized.
- A same-user process can invoke the helper and cause a prompt. The user reads
  and approves the operation and target in that system prompt. Availability
  attacks from another process under the same account remain an operating
  system account concern.

## Troubleshooting

| Result | Meaning and action |
| --- | --- |
| `helper_signing_invalid` | The installed application does not have the required identity or entitlement. Re-download the official artifact and run its verifier. |
| `user_presence_cancelled` | The user cancelled the system prompt. No protected operation was dispatched; retry only when the request is expected. |
| `user_presence_unavailable` | macOS could not provide the configured user-presence policy. Confirm an interactive login and available login authentication, then retry. |
| `session_reauthorization_required` | Refresh or stored-session validation failed closed. Run the supported authorization flow again. |
| `operation_approval_required` | KDCube requires request-bound browser approval. Approve the exact request and retry with the same invocation identifier. |
| `operation_denied` | The live card, operation policy, target, or application does not admit the request. Review authority in Connection Hub. |
| `session_busy` | Another helper process owns the same session lock. Wait for it to finish and retry. |

OSStatus `-34018`, `-25291`, and `-67674` during source experiments indicate
an invalid entitlement, unavailable Data Protection Keychain context, or an
incompatible legacy-Keychain shape. They are not solved by weakening the item
to an ordinary Keychain record. Release users should replace an invalid
artifact rather than re-sign it.

## Maintainer Signing And Interactive Acceptance

This section is for contributors producing or validating the release. It is
not part of end-user setup.

The Team ID is Apple's 10-character membership identifier, not an organization
name, account nickname, or certificate label. Find it under **Membership
details** in the Apple Developer account. Apple documents the identifier in
its [Team ID glossary](https://developer.apple.com/help/glossary/team-id/).

Before the first build for a team:

1. Register the explicit macOS App ID
   `tech.kdcube.connection-hub-presence-helper`.
2. Enable Keychain Sharing for that App ID and authorize the access group
   `<TEAM_ID>.tech.kdcube.connection-hub-presence-helper`. Apple explains how
   access groups and the team-plus-bundle application identifier control Data
   Protection Keychain sharing in
   [Sharing access to keychain items among a collection of apps](https://developer.apple.com/documentation/security/sharing-access-to-keychain-items-among-a-collection-of-apps).
3. Create an Apple Development certificate and **Mac App Development** profile
   for the interactive test. Apple's
   [development-profile procedure](https://developer.apple.com/help/account/provisioning-profiles/create-a-development-provisioning-profile)
   lists the required App ID, certificate, and registered Mac.
4. Create a Developer ID Application certificate and matching Developer ID
   profile for direct distribution. Apple documents the certificate and
   profile role in
   [Developer ID certificates](https://developer.apple.com/help/account/certificates/create-developer-id-certificates/).
5. Configure `notarytool` credentials for production. Apple requires a
   Developer ID signature, hardened runtime, secure timestamp, and notarization
   for current software distributed outside the Mac App Store; see
   [Notarizing macOS software before distribution](https://developer.apple.com/documentation/security/notarizing_macos_software_before_distribution).

The production application requires:

- the explicit bundle ID `tech.kdcube.connection-hub-presence-helper`;
- a real 10-character uppercase alphanumeric Apple Team ID;
- a Developer ID Application identity available to `codesign`;
- a matching provisioning profile whose application identifier and first
  Keychain access group are
  `<TEAM_ID>.tech.kdcube.connection-hub-presence-helper`;
- a `notarytool` keychain profile for the same release authority.

Inspect the current machine before building:

```bash
security find-identity -v -p codesigning

find "$HOME/Library/Developer/Xcode/UserData/Provisioning Profiles" \
  "$HOME/Library/MobileDevice/Provisioning Profiles" \
  -type f 2>/dev/null
```

`security find-identity` must list the exact identity passed to the script. A
result of `0 valid identities found` means the certificate and its private key
are not available in the current user's keychain. Inspect a candidate profile
before building:

```bash
PROFILE="<actual-profile-path>"
PROFILE_PLIST="$(mktemp /private/tmp/connection-hub-profile.XXXXXX)"
security cms -D -i "$PROFILE" > "$PROFILE_PLIST"

/usr/libexec/PlistBuddy -c 'Print :TeamIdentifier:0' "$PROFILE_PLIST"
/usr/libexec/PlistBuddy -c 'Print :Entitlements:com.apple.application-identifier' "$PROFILE_PLIST"
/usr/libexec/PlistBuddy -c 'Print :Entitlements:keychain-access-groups:0' "$PROFILE_PLIST"

rm -f "$PROFILE_PLIST"
```

The three values must be the Team ID and the same
`<TEAM_ID>.tech.kdcube.connection-hub-presence-helper` application/access-group
identifier. A literal `/path/to/helper.provisionprofile` is only an example and
will be rejected because it is not a file.

For the real prompt acceptance, an Apple Development identity and matching
macOS development profile may be used. Build and run only while the tester is
present:

```bash
cd products/connection-hub/packages/connection-hub-presence-helper-macos

scripts/build-interactive-check.sh \
  --output /private/tmp/connection-hub-presence-rust-interactive \
  --signing-identity "Apple Development: <name> (<TEAM_ID>)" \
  --team-id <TEAM_ID> \
  --provisioning-profile <actual-profile-path>

/private/tmp/connection-hub-presence-rust-interactive/ConnectionHubPresenceInteractiveCheck.app/Contents/MacOS/connection-hub-presence-interactive-check --run
```

The tester cancels the first inspect prompt, approves the second inspect
prompt, cancels the changed reload prompt, approves that reload, and approves
cleanup. The loopback fixture must observe zero, one, zero, and one requests,
including the exact method, path, `{}` body, and disposable credential.
Cleanup failure must produce a nonzero exit.

Production packaging uses the Developer ID identity and notarization profile:

```bash
scripts/build-app.sh \
  --version <YYYY.MM.DD.HHMM> \
  --output /private/tmp/connection-hub-presence-release \
  --signing-identity "Developer ID Application: <organization> (<TEAM_ID>)" \
  --team-id <TEAM_ID> \
  --provisioning-profile <actual-profile-path> \
  --notary-profile <notarytool-keychain-profile>
```

`--adhoc-test` exists only for automated packaging and lifecycle tests. An
ad-hoc artifact has no production Keychain access group and is never a release
candidate.
