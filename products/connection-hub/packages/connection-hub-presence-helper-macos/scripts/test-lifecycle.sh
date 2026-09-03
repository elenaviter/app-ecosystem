#!/bin/bash
set -euo pipefail

PACKAGE_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD="$PACKAGE_ROOT/scripts/build-app.sh"
MANAGE="$PACKAGE_ROOT/scripts/manage-user-install.sh"
VERSION_ONE="2026.09.03.2358"
VERSION_TWO="2026.09.03.2359"
APP_NAME="ConnectionHubPresenceHelper.app"

workspace="$(mktemp -d /private/tmp/connection-hub-presence-helper-lifecycle-XXXXXX)"
install_root="$workspace/install"
artifacts_one="$workspace/artifacts-one"
artifacts_two="$workspace/artifacts-two"
tampered="$workspace/tampered/$APP_NAME"

cleanup() {
    rm -rf "$workspace"
}
trap cleanup EXIT

"$BUILD" --version "$VERSION_ONE" --output "$artifacts_one" --adhoc-test
"$BUILD" --version "$VERSION_TWO" --output "$artifacts_two" --adhoc-test

"$MANAGE" install \
    --app "$artifacts_one/$APP_NAME" \
    --test-root "$install_root" \
    --allow-adhoc-test

current_version="$(plutil -extract KDCubeReleaseVersion raw "$install_root/current/Contents/Info.plist")"
if [[ "$current_version" != "$VERSION_ONE" ]]; then
    printf 'Initial activation selected the wrong version.\n' >&2
    exit 1
fi

mkdir -p "$(dirname "$tampered")"
ditto "$artifacts_two/$APP_NAME" "$tampered"
printf '\0' >> "$tampered/Contents/MacOS/connection-hub-presence-helper"
if "$MANAGE" upgrade \
    --app "$tampered" \
    --test-root "$install_root" \
    --allow-adhoc-test >/dev/null 2>&1; then
    printf 'A tampered upgrade was accepted.\n' >&2
    exit 1
fi
current_version="$(plutil -extract KDCubeReleaseVersion raw "$install_root/current/Contents/Info.plist")"
if [[ "$current_version" != "$VERSION_ONE" ]]; then
    printf 'A rejected upgrade changed the active version.\n' >&2
    exit 1
fi

"$MANAGE" upgrade \
    --app "$artifacts_two/$APP_NAME" \
    --test-root "$install_root" \
    --allow-adhoc-test
current_version="$(plutil -extract KDCubeReleaseVersion raw "$install_root/current/Contents/Info.plist")"
if [[ "$current_version" != "$VERSION_TWO" ]]; then
    printf 'Upgrade activation selected the wrong version.\n' >&2
    exit 1
fi
if [[ ! -d "$install_root/versions/$VERSION_ONE/$APP_NAME" ]]; then
    printf 'Upgrade did not retain the prior verified artifact.\n' >&2
    exit 1
fi

if "$MANAGE" uninstall \
    --test-root "$install_root" \
    --allow-adhoc-test >/dev/null 2>&1; then
    printf 'Ad-hoc uninstall unexpectedly bypassed the protected purge.\n' >&2
    exit 1
fi
if [[ ! -L "$install_root/current" ]]; then
    printf 'Failed protected purge removed the installed helper.\n' >&2
    exit 1
fi

"$MANAGE" uninstall \
    --test-root "$install_root" \
    --allow-adhoc-test \
    --skip-credential-purge-for-test
if [[ -e "$install_root" ]]; then
    printf 'Test uninstall left the isolated install root behind.\n' >&2
    exit 1
fi

printf 'PASS: signed-artifact verification, atomic upgrade, rollback preservation, and fail-closed uninstall.\n'
