#!/bin/bash
set -euo pipefail

BUNDLE_ID="tech.kdcube.connection-hub-presence-helper"
APP_NAME="ConnectionHubPresenceHelper.app"
DEFAULT_ROOT="$HOME/Library/Application Support/KDCube/ConnectionHubPresenceHelper"
SCRIPT_ROOT="$(cd "$(dirname "$0")" && pwd)"
VERIFIER="$SCRIPT_ROOT/verify-app.sh"

action="${1:-}"
if [[ -z "$action" ]]; then
    printf 'Action is required: install, upgrade, or uninstall.\n' >&2
    exit 2
fi
shift

app=""
expected_team_id=""
root="$DEFAULT_ROOT"
allow_adhoc_test=false
skip_credential_purge_for_test=false

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
        --test-root)
            root="${2:-}"
            shift 2
            ;;
        --allow-adhoc-test)
            allow_adhoc_test=true
            shift
            ;;
        --skip-credential-purge-for-test)
            skip_credential_purge_for_test=true
            shift
            ;;
        *)
            printf 'Unknown argument: %s\n' "$1" >&2
            exit 2
            ;;
    esac
done

if [[ "$root" != "$DEFAULT_ROOT" ]]; then
    case "$root" in
        /private/tmp/connection-hub-presence-helper-lifecycle-*) ;;
        *)
            printf 'Test roots must use the isolated /private/tmp lifecycle prefix.\n' >&2
            exit 2
            ;;
    esac
    if [[ "$allow_adhoc_test" != true ]]; then
        printf 'A test root requires --allow-adhoc-test.\n' >&2
        exit 2
    fi
fi
if [[ "$skip_credential_purge_for_test" == true && "$root" == "$DEFAULT_ROOT" ]]; then
    printf 'Credential purge cannot be skipped for a real user install.\n' >&2
    exit 2
fi

verify_app() {
    local candidate="$1"
    if [[ "$allow_adhoc_test" == true ]]; then
        "$VERIFIER" --app "$candidate" --allow-adhoc-test
    else
        "$VERIFIER" --app "$candidate" --expected-team-id "$expected_team_id"
    fi
}

install_candidate() {
    local candidate="$1"
    verify_app "$candidate"
    if [[ -L "$root/current" ]]; then
        verify_app "$root/current"
    fi
    local version
    version="$(plutil -extract KDCubeReleaseVersion raw "$candidate/Contents/Info.plist")"
    local versions="$root/versions"
    local destination="$versions/$version/$APP_NAME"
    local staging="$root/.staging-$version-$$"
    local next_link="$root/.current-$$"

    if [[ -L "$root" || -L "$versions" ]]; then
        printf 'The helper install root cannot be a symbolic link.\n' >&2
        exit 1
    fi
    mkdir -p "$versions"
    chmod 0700 "$root" "$versions"
    if [[ -e "$versions/$version" || -L "$versions/$version" ]]; then
        printf 'Version is already installed.\n' >&2
        exit 1
    fi
    rm -rf "$staging"
    mkdir -p "$staging"
    ditto "$candidate" "$staging/$APP_NAME"
    verify_app "$staging/$APP_NAME"
    chmod -R go-w "$staging/$APP_NAME"
    mkdir -p "$versions/$version"
    mv "$staging/$APP_NAME" "$destination"
    rmdir "$staging"

    ln -s "versions/$version/$APP_NAME" "$next_link"
    mv -h -f "$next_link" "$root/current"
    printf 'Installed %s\n' "$version"
}

case "$action" in
    install)
        if [[ -z "$app" ]]; then
            printf '%s requires --app.\n' "$action" >&2
            exit 2
        fi
        if [[ -e "$root/current" || -L "$root/current" ]]; then
            printf 'A helper is already installed; use upgrade.\n' >&2
            exit 1
        fi
        install_candidate "$app"
        ;;
    upgrade)
        if [[ -z "$app" ]]; then
            printf '%s requires --app.\n' "$action" >&2
            exit 2
        fi
        if [[ ! -L "$root/current" ]]; then
            printf 'No installed helper was found; use install.\n' >&2
            exit 1
        fi
        install_candidate "$app"
        ;;
    uninstall)
        current="$root/current"
        if [[ ! -L "$current" ]]; then
            printf 'No installed helper was found.\n' >&2
            exit 1
        fi
        verify_app "$current"
        helper="$current/Contents/MacOS/connection-hub-presence-helper"
        if [[ "$skip_credential_purge_for_test" != true ]]; then
            request_id="$(uuidgen | tr '[:upper:]' '[:lower:]')"
            request="{\"protocol_version\":1,\"request_id\":\"$request_id\",\"command\":\"purge_all_sessions\"}"
            if ! printf '%s' "$request" | "$helper" >/dev/null; then
                printf 'Protected-session purge failed; the helper remains installed.\n' >&2
                exit 1
            fi
        fi
        rm -rf "$root"
        printf 'Uninstalled %s\n' "$BUNDLE_ID"
        ;;
    *)
        printf 'Unknown action: %s\n' "$action" >&2
        exit 2
        ;;
esac
