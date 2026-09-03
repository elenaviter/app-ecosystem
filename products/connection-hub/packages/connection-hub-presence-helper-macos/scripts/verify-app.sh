#!/bin/bash
set -euo pipefail

BUNDLE_ID="tech.kdcube.connection-hub-presence-helper"
app=""
expected_team_id=""
allow_adhoc_test=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --app)
            app="${2:-}"
            shift 2
            ;;
        --expected-team-id)
            expected_team_id="${2:-}"
            shift 2
            ;;
        --allow-adhoc-test)
            allow_adhoc_test=true
            shift
            ;;
        *)
            printf 'Unknown argument: %s\n' "$1" >&2
            exit 2
            ;;
    esac
done

if [[ ! -d "$app" ]]; then
    printf 'App bundle does not exist.\n' >&2
    exit 2
fi
codesign --verify --deep --strict --verbose=2 "$app"

actual_id="$(plutil -extract CFBundleIdentifier raw "$app/Contents/Info.plist")"
version="$(plutil -extract KDCubeReleaseVersion raw "$app/Contents/Info.plist")"
configured_access_group="$(plutil -extract KDCubeKeychainAccessGroup raw "$app/Contents/Info.plist")"
if [[ "$actual_id" != "$BUNDLE_ID" ]]; then
    printf 'Unexpected bundle identifier.\n' >&2
    exit 1
fi
if [[ ! "$version" =~ ^[0-9]{4}\.[0-9]{2}\.[0-9]{2}\.[0-9]{4}$ ]]; then
    printf 'Unexpected bundle version.\n' >&2
    exit 1
fi

signature="$(codesign -d --verbose=4 "$app" 2>&1)"
team_id="$(printf '%s\n' "$signature" | awk -F= '/^TeamIdentifier=/{print $2; exit}')"
if [[ "$allow_adhoc_test" == true ]]; then
    if [[ -n "$expected_team_id" ]]; then
        printf 'Ad-hoc verification cannot assert a production Team ID.\n' >&2
        exit 2
    fi
    if [[ -n "$configured_access_group" ]]; then
        printf 'Ad-hoc artifacts cannot advertise a production Keychain access group.\n' >&2
        exit 1
    fi
else
    if [[ ! "$expected_team_id" =~ ^[A-Z0-9]{10}$ || "$team_id" != "$expected_team_id" ]]; then
        printf 'The helper Team ID does not match the required production identity.\n' >&2
        exit 1
    fi
    entitlements="$(codesign -d --entitlements :- "$app" 2>/dev/null)"
    expected_access_group="$expected_team_id.$BUNDLE_ID"
    if [[ "$configured_access_group" != "$expected_access_group" || "$entitlements" != *"$expected_access_group"* ]]; then
        printf 'The production Keychain access-group entitlement is missing.\n' >&2
        exit 1
    fi
    if [[ "$signature" != *"Authority=Developer ID Application:"* ]]; then
        printf 'The helper is not signed with a Developer ID Application identity.\n' >&2
        exit 1
    fi
    embedded="$app/Contents/embedded.provisionprofile"
    if [[ ! -f "$embedded" ]]; then
        printf 'The production provisioning profile is missing.\n' >&2
        exit 1
    fi
    profile_plist="$(mktemp /private/tmp/connection-hub-presence-profile.XXXXXX)"
    trap 'rm -f "$profile_plist"' EXIT
    security cms -D -i "$embedded" > "$profile_plist"
    profile_team_id="$(/usr/libexec/PlistBuddy -c 'Print :TeamIdentifier:0' "$profile_plist")"
    profile_application_id="$(/usr/libexec/PlistBuddy -c 'Print :Entitlements:com.apple.application-identifier' "$profile_plist")"
    profile_access_group="$(/usr/libexec/PlistBuddy -c 'Print :Entitlements:keychain-access-groups:0' "$profile_plist")"
    if [[ "$profile_team_id" != "$expected_team_id" || "$profile_application_id" != "$expected_access_group" || "$profile_access_group" != "$expected_access_group" ]]; then
        printf 'The embedded provisioning profile does not authorize this helper.\n' >&2
        exit 1
    fi
    xcrun stapler validate "$app" >/dev/null
    spctl --assess --type execute --verbose=4 "$app" >/dev/null
fi

if find "$app" -type f \( -perm -002 -o -perm -020 \) -print -quit | grep -q .; then
    printf 'The app contains a group- or world-writable file.\n' >&2
    exit 1
fi
printf 'Verified %s %s\n' "$BUNDLE_ID" "$version"
