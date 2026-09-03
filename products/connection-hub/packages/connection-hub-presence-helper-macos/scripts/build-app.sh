#!/bin/bash
set -euo pipefail
export LANG=C
export LC_ALL=C

PACKAGE_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUNDLE_ID="tech.kdcube.connection-hub-presence-helper"
APP_NAME="ConnectionHubPresenceHelper.app"

version=""
output=""
identity=""
team_id=""
provisioning_profile=""
notary_profile=""
adhoc_test=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --version)
            version="${2:-}"
            shift 2
            ;;
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
        --notary-profile)
            notary_profile="${2:-}"
            shift 2
            ;;
        --adhoc-test)
            adhoc_test=true
            shift
            ;;
        *)
            printf 'Unknown argument: %s\n' "$1" >&2
            exit 2
            ;;
    esac
done

if [[ ! "$version" =~ ^[0-9]{4}\.[0-9]{2}\.[0-9]{2}\.[0-9]{4}$ ]]; then
    printf 'Version must use YYYY.MM.DD.HHMM.\n' >&2
    exit 2
fi
if [[ -z "$output" ]]; then
    printf '%s\n' '--output is required.' >&2
    exit 2
fi
if [[ "$adhoc_test" == false ]]; then
    preflight_failed=0
    available_identities="$(security find-identity -v -p codesigning 2>&1 || true)"
    if [[ -z "$identity" ]]; then
        printf '%s\n' '--signing-identity is required for production packaging.' >&2
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
        printf '%s\n' '--provisioning-profile is required for production packaging.' >&2
        preflight_failed=1
    elif [[ ! -f "$provisioning_profile" ]]; then
        printf 'Provisioning profile does not exist: %s\n' "$provisioning_profile" >&2
        preflight_failed=1
    fi
    if [[ -z "$notary_profile" ]]; then
        printf '%s\n' '--notary-profile is required for production packaging.' >&2
        preflight_failed=1
    fi
    if [[ "$preflight_failed" -ne 0 ]]; then
        exit 2
    fi
fi

mkdir -p "$output"
output="$(cd "$output" && pwd)"
work="$output/.build-$version"
app="$output/$APP_NAME"
archive="$output/ConnectionHubPresenceHelper-$version.zip"
cargo_target="$work/cargo-target"

rm -rf "$work" "$app" "$archive"
mkdir -p \
    "$app/Contents/MacOS" \
    "$app/Contents/Resources"

short_version="${version%.*}"
build_version="${version//./}"
keychain_access_group=""
if [[ "$adhoc_test" == false ]]; then
    keychain_access_group="$team_id.$BUNDLE_ID"
fi

env \
    CARGO_TARGET_DIR="$cargo_target" \
    KDCUBE_HELPER_VERSION="$version" \
    KDCUBE_KEYCHAIN_ACCESS_GROUP="$keychain_access_group" \
    MACOSX_DEPLOYMENT_TARGET=13.0 \
    cargo build \
        --locked \
        --manifest-path "$PACKAGE_ROOT/Cargo.toml" \
        --release \
        --bin connection-hub-presence-helper

cp "$cargo_target/release/connection-hub-presence-helper" "$app/Contents/MacOS/"
cp "$PACKAGE_ROOT/scripts/manage-user-install.sh" "$app/Contents/Resources/"
cp "$PACKAGE_ROOT/scripts/verify-app.sh" "$app/Contents/Resources/"
chmod 0755 "$app/Contents/MacOS/connection-hub-presence-helper"
chmod 0755 "$app/Contents/Resources/"*.sh

sed \
    -e "s/__VERSION__/$version/g" \
    -e "s/__SHORT_VERSION__/$short_version/g" \
    -e "s/__BUILD_VERSION__/$build_version/g" \
    -e "s/__KEYCHAIN_ACCESS_GROUP__/$keychain_access_group/g" \
    "$PACKAGE_ROOT/Packaging/Info.plist.in" > "$app/Contents/Info.plist"
plutil -lint "$app/Contents/Info.plist" >/dev/null

if [[ "$adhoc_test" == true ]]; then
    codesign \
        --force \
        --sign - \
        --identifier "$BUNDLE_ID" \
        "$app"
    signing_mode="adhoc-test"
    notarized=false
else
    profile_plist="$work/provisioning-profile.plist"
    security cms -D -i "$provisioning_profile" > "$profile_plist"
    profile_team_id="$(/usr/libexec/PlistBuddy -c 'Print :TeamIdentifier:0' "$profile_plist")"
    profile_application_id="$(/usr/libexec/PlistBuddy -c 'Print :Entitlements:com.apple.application-identifier' "$profile_plist")"
    profile_access_group="$(/usr/libexec/PlistBuddy -c 'Print :Entitlements:keychain-access-groups:0' "$profile_plist")"
    if [[ "$profile_team_id" != "$team_id" || "$profile_application_id" != "$keychain_access_group" || "$profile_access_group" != "$keychain_access_group" ]]; then
        printf 'The provisioning profile does not authorize the required app and Keychain access group.\n' >&2
        exit 1
    fi
    cp "$provisioning_profile" "$app/Contents/embedded.provisionprofile"
    entitlements="$work/production.entitlements"
    sed "s/__TEAM_ID__/$team_id/g" \
        "$PACKAGE_ROOT/Packaging/production.entitlements.in" > "$entitlements"
    plutil -lint "$entitlements" >/dev/null
    codesign \
        --force \
        --options runtime \
        --timestamp \
        --sign "$identity" \
        --entitlements "$entitlements" \
        "$app"
    signing_mode="developer-id"
    submission="$work/notarization.zip"
    ditto -c -k --keepParent "$app" "$submission"
    xcrun notarytool submit "$submission" \
        --keychain-profile "$notary_profile" \
        --wait
    xcrun stapler staple "$app"
    xcrun stapler validate "$app"
    spctl --assess --type execute --verbose=4 "$app"
    "$PACKAGE_ROOT/scripts/verify-app.sh" \
        --app "$app" \
        --expected-team-id "$team_id"
    notarized=true
fi

codesign --verify --deep --strict --verbose=2 "$app"
ditto -c -k --keepParent "$app" "$archive"

app_hash="$(find "$app" -type f -exec shasum -a 256 {} \; | LC_ALL=C sort | shasum -a 256 | awk '{print $1}')"
archive_hash="$(shasum -a 256 "$archive" | awk '{print $1}')"
lock_hash="$(shasum -a 256 "$PACKAGE_ROOT/Cargo.lock" | awk '{print $1}')"
cat > "$output/manifest-$version.txt" <<EOF
bundle_id=$BUNDLE_ID
version=$version
implementation=rust
signing_mode=$signing_mode
notarized=$notarized
team_id=${team_id:-none}
cargo_lock_sha256=$lock_hash
app_tree_sha256=$app_hash
archive_sha256=$archive_hash
EOF

rm -rf "$work"
printf 'Built %s\n' "$app"
printf 'Archive SHA-256: %s\n' "$archive_hash"
