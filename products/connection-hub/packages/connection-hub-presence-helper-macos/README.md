# Connection Hub macOS Presence Helper

This pre-release Rust application keeps a delegated KDCube management OAuth
session inside a separately signed macOS helper. It performs one compiled,
request-bound management operation after macOS user presence and returns only
secret-free evidence.

The helper is not yet integrated into the shared `connection-hub` command. Its
supported release begins only after the signing, notarization, interactive
acceptance, clean-account lifecycle, and command-integration gates pass. The
canonical user, security, installation, signing, and acceptance guide is:

[Protect KDCube Management On macOS With User Presence](../../../../docs/connection-hub/macos-user-presence-helper.md)

Production artifacts require Developer ID signing, the exact provisioned
Keychain access group, notarization, Gatekeeper acceptance, and the real
interactive test. Ad-hoc artifacts are limited to automated packaging and
lifecycle checks.
