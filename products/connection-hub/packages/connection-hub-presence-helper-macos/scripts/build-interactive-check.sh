#!/bin/bash
set -euo pipefail
export LANG=C
export LC_ALL=C

PACKAGE_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP_NAME="ConnectionHubPresenceInteractiveCheck.app"
BUNDLE_ID="tech.kdcube.connection-hub-presence-helper"
output=""
identity=""
team_id=""
provisioning_profile=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --output)
            output="${2:-}"
            shift 2
            ;;
        --signing-identity)
            identity="${2:-}"
            shift 2
            ;;
        --team-id)
            team_id="${2:-}"
            shift 2
            ;;
        --provisioning-profile)
            provisioning_profile="${2:-}"
            shift 2
            ;;
        *)
            printf 'Unknown argument: %s\n' "$1" >&2
            exit 2
            ;;
    esac
done

if [[ -z "$output" ]]; then
    printf '%s\n' '--output is required.' >&2
    exit 2
fi
preflight_failed=0
available_identities="$(security find-identity -v -p codesigning 2>&1 || true)"
if [[ -z "$identity" ]]; then
    printf '%s\n' '--signing-identity is required.' >&2
    preflight_failed=1
elif ! printf '%s\n' "$available_identities" | /usr/bin/grep -Fq -- "$identity"; then
    printf 'Signing identity is not available in the current keychain: %s\n' "$identity" >&2
    printf '%s\n' 'Available code-signing identities:' >&2
    printf '%s\n' "$available_identities" >&2
    preflight_failed=1
fi
if [[ ! "$team_id" =~ ^[A-Z0-9]{10}$ ]]; then
    printf '%s\n' '--team-id must be the 10-character uppercase alphanumeric Apple Team ID.' >&2
    preflight_failed=1
fi
if [[ -z "$provisioning_profile" ]]; then
    printf '%s\n' '--provisioning-profile is required.' >&2
    preflight_failed=1
elif [[ ! -f "$provisioning_profile" ]]; then
    printf 'Provisioning profile does not exist: %s\n' "$provisioning_profile" >&2
    preflight_failed=1
fi
if [[ "$preflight_failed" -ne 0 ]]; then
    exit 2
fi

mkdir -p "$output"
output="$(cd "$output" && pwd)"
work="$output/.interactive-build"
app="$output/$APP_NAME"
target="$work/cargo-target"
keychain_access_group="$team_id.$BUNDLE_ID"
rm -rf "$work" "$app"
mkdir -p "$work" "$app/Contents/MacOS"

profile_plist="$work/provisioning-profile.plist"
security cms -D -i "$provisioning_profile" > "$profile_plist"
profile_team_id="$(/usr/libexec/PlistBuddy -c 'Print :TeamIdentifier:0' "$profile_plist")"
profile_application_id="$(/usr/libexec/PlistBuddy -c 'Print :Entitlements:com.apple.application-identifier' "$profile_plist")"
profile_access_group="$(/usr/libexec/PlistBuddy -c 'Print :Entitlements:keychain-access-groups:0' "$profile_plist")"
if [[ "$profile_team_id" != "$team_id" || "$profile_application_id" != "$keychain_access_group" || "$profile_access_group" != "$keychain_access_group" ]]; then
    printf 'The provisioning profile does not authorize the helper app and Keychain access group.\n' >&2
    exit 1
fi

env \
    CARGO_TARGET_DIR="$target" \
    KDCUBE_HELPER_VERSION="interactive-check" \
    KDCUBE_KEYCHAIN_ACCESS_GROUP="$keychain_access_group" \
    MACOSX_DEPLOYMENT_TARGET=13.0 \
    cargo build \
        --locked \
        --manifest-path "$PACKAGE_ROOT/Cargo.toml" \
        --release \
        --features interactive-check \
        --bin connection-hub-presence-interactive-check

cp "$target/release/connection-hub-presence-interactive-check" "$app/Contents/MacOS/"
sed "s/__KEYCHAIN_ACCESS_GROUP__/$keychain_access_group/g" \
    "$PACKAGE_ROOT/Packaging/InteractiveCheckInfo.plist.in" > "$app/Contents/Info.plist"
cp "$provisioning_profile" "$app/Contents/embedded.provisionprofile"
chmod 0755 "$app/Contents/MacOS/connection-hub-presence-interactive-check"
plutil -lint "$app/Contents/Info.plist" >/dev/null
entitlements="$work/interactive.entitlements"
sed "s/__TEAM_ID__/$team_id/g" \
    "$PACKAGE_ROOT/Packaging/production.entitlements.in" > "$entitlements"
plutil -lint "$entitlements" >/dev/null
codesign \
    --force \
    --options runtime \
    --sign "$identity" \
    --entitlements "$entitlements" \
    "$app"
codesign --verify --deep --strict --verbose=2 "$app"
signed_entitlements="$(codesign -d --entitlements :- "$app" 2>/dev/null)"
if [[ "$signed_entitlements" != *"$keychain_access_group"* ]]; then
    printf 'The signed interactive app is missing the required Keychain access group.\n' >&2
    exit 1
fi
rm -rf "$work"
printf 'Built %s\n' "$app"
printf 'Run only while the operator is present:\n%s/Contents/MacOS/connection-hub-presence-interactive-check --run\n' "$app"
