#!/bin/sh
# Root-only provisioner for the one-way GP legacy clean-remove bridge.
#
# It reads the cleaner directly from the exact commit object in the fixed,
# install-user-owned candidate repository prepared by legacy-bootstrap.sh.
# It does not fetch, checkout, update, restore, inspect vault contents, or
# alter any legacy GP service, helper, sudoers, checkout or state.
set -eu
umask 077

PATH='/usr/sbin:/usr/bin:/sbin:/bin'
export PATH

readonly CLEANER_DIRECTORY='/usr/local/libexec/gp-control-plane'
readonly CLEANER_PATH="$CLEANER_DIRECTORY/gp-clean-remove-root"
readonly PREFLIGHT_PATH="$CLEANER_DIRECTORY/gp-clean-remove-preflight"
readonly MANIFEST_PATH="$CLEANER_DIRECTORY/gp-clean-remove-root.manifest"

CLEANER_TEMP=''
PREFLIGHT_TEMP=''
CLEANER_INSTALL_TEMP=''
PREFLIGHT_INSTALL_TEMP=''
MANIFEST_TEMP=''

die() {
    printf 'gp-clean-remove-provision-root: %s\n' "$1" >&2
    exit 126
}

cleanup() {
    [ -z "$CLEANER_TEMP" ] || rm -f -- "$CLEANER_TEMP"
    [ -z "$PREFLIGHT_TEMP" ] || rm -f -- "$PREFLIGHT_TEMP"
    [ -z "$CLEANER_INSTALL_TEMP" ] || rm -f -- "$CLEANER_INSTALL_TEMP"
    [ -z "$PREFLIGHT_INSTALL_TEMP" ] || rm -f -- "$PREFLIGHT_INSTALL_TEMP"
    [ -z "$MANIFEST_TEMP" ] || rm -f -- "$MANIFEST_TEMP"
}

usage() {
    printf '%s\n' \
        'usage: gp-clean-remove-provision-root.sh --install-user USER --candidate-sha 40-lowercase-hex --cleaner-sha256 64-lowercase-hex --preflight-sha256 64-lowercase-hex' >&2
    exit 64
}

is_sha40() {
    case "${1:-}" in ????????????????????????????????????????) ;; *) return 1 ;; esac
    case "$1" in *[!0-9a-f]*) return 1 ;; esac
}

is_sha256() {
    case "${1:-}" in ????????????????????????????????????????????????????????????????) ;; *) return 1 ;; esac
    case "$1" in *[!0-9a-f]*) return 1 ;; esac
}

safe_root_directory() {
    path=$1
    [ ! -e "$path" ] && [ ! -L "$path" ] && return 0
    [ -d "$path" ] && [ ! -L "$path" ] || die "root cleaner directory is unsafe: $path"
    [ "$(readlink -f -- "$path" 2>/dev/null || true)" = "$path" ] || die "root cleaner directory is non-canonical: $path"
    [ "$(stat -c '%u:%g' "$path" 2>/dev/null || true)" = '0:0' ] || die "root cleaner directory is not root-owned: $path"
    mode=$(stat -c '%a' "$path" 2>/dev/null || true)
    case "$mode" in ??[2367]|?[2367]?|[2367]???) die "root cleaner directory is group/world-writable: $path" ;; esac
}

safe_install_user_directory() {
    path=$1; uid=$2; label=$3
    [ -d "$path" ] && [ ! -L "$path" ] || die "$label is unsafe"
    [ "$(readlink -f -- "$path" 2>/dev/null || true)" = "$path" ] || die "$label is non-canonical"
    [ "$(stat -c '%u' "$path" 2>/dev/null || true)" = "$uid" ] || die "$label owner changed"
}

require_candidate_repository() {
    install_uid=$(id -u "$INSTALL_USER") || die 'cannot resolve install-user uid'
    install_home=$(getent passwd "$INSTALL_USER" | cut -d: -f6) || die 'cannot resolve install-user home'
    [ -n "$install_home" ] || die 'cannot resolve install-user home'
    [ "$install_home" = "$(readlink -f -- "$install_home" 2>/dev/null || true)" ] || die 'install-user home is non-canonical'
    safe_install_user_directory "$install_home" "$install_uid" 'install-user home'
    candidate_repository="$install_home/.cache/gp-control-plane/clean-handoff/candidate-$CANDIDATE_SHA"
    safe_install_user_directory "$candidate_repository" "$install_uid" 'pinned candidate repository'
    GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null \
        GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0="$candidate_repository" \
        git -C "$candidate_repository" cat-file -e "$CANDIDATE_SHA^{commit}" 2>/dev/null \
        || die 'pinned candidate SHA is not present as a commit object'
}

extract_verified_cleaner() {
    CLEANER_TEMP=$(mktemp /run/gp-clean-remove.provision.XXXXXX) || die 'cannot create root-private cleaner staging file'
    PREFLIGHT_TEMP=$(mktemp /run/gp-clean-remove.preflight.XXXXXX) || die 'cannot create root-private preflight staging file'
    GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null \
        GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0="$candidate_repository" \
        git -C "$candidate_repository" show "$CANDIDATE_SHA:scripts/gp-clean-remove-root.sh" > "$CLEANER_TEMP" \
        || die 'cannot read root cleaner from the pinned candidate commit'
    [ "$(sha256sum "$CLEANER_TEMP" | awk '{print $1}')" = "$CLEANER_SHA256" ] \
        || die 'pinned candidate root cleaner hash does not match'
    sh -n "$CLEANER_TEMP" || die 'pinned candidate root cleaner has invalid shell syntax'
    GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null \
        GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0="$candidate_repository" \
        git -C "$candidate_repository" show "$CANDIDATE_SHA:scripts/gp-clean-remove-preflight.sh" > "$PREFLIGHT_TEMP" \
        || die 'cannot read clean-remove preflight from the pinned candidate commit'
    [ "$(sha256sum "$PREFLIGHT_TEMP" | awk '{print $1}')" = "$PREFLIGHT_SHA256" ] \
        || die 'pinned candidate clean-remove preflight hash does not match'
    sh -n "$PREFLIGHT_TEMP" || die 'pinned candidate clean-remove preflight has invalid shell syntax'
}

publish_cleaner() {
    safe_root_directory "$CLEANER_DIRECTORY"
    [ -d "$CLEANER_DIRECTORY" ] || install -d -m 0755 -o root -g root "$CLEANER_DIRECTORY" || die 'cannot create root cleaner directory'
    CLEANER_INSTALL_TEMP=$(mktemp "$CLEANER_DIRECTORY/.gp-clean-remove-root.XXXXXX") || die 'cannot stage root cleaner publication'
    PREFLIGHT_INSTALL_TEMP=$(mktemp "$CLEANER_DIRECTORY/.gp-clean-remove-preflight.XXXXXX") || die 'cannot stage clean-remove preflight publication'
    MANIFEST_TEMP=$(mktemp "$CLEANER_DIRECTORY/.gp-clean-remove-root.manifest.XXXXXX") || die 'cannot stage root cleaner manifest'
    install -m 0700 -o root -g root "$CLEANER_TEMP" "$CLEANER_INSTALL_TEMP" || die 'cannot stage verified root cleaner'
    install -m 0755 -o root -g root "$PREFLIGHT_TEMP" "$PREFLIGHT_INSTALL_TEMP" || die 'cannot stage verified clean-remove preflight'
    printf '%s\n' \
        "candidate_sha=$CANDIDATE_SHA" \
        "cleaner_sha256=$CLEANER_SHA256" \
        "preflight_sha256=$PREFLIGHT_SHA256" \
        "cleaner_path=$CLEANER_PATH" \
        "preflight_path=$PREFLIGHT_PATH" \
        > "$MANIFEST_TEMP" || die 'cannot write root cleaner manifest'
    chown root:root "$MANIFEST_TEMP" || die 'cannot own root cleaner manifest'
    chmod 0600 "$MANIFEST_TEMP" || die 'cannot protect root cleaner manifest'
    [ "$(stat -c '%u:%g:%a' "$CLEANER_INSTALL_TEMP" 2>/dev/null || true)" = '0:0:700' ] \
        || die 'staged root cleaner ownership or mode is unsafe'
    [ "$(stat -c '%u:%g:%a' "$PREFLIGHT_INSTALL_TEMP" 2>/dev/null || true)" = '0:0:755' ] \
        || die 'staged clean-remove preflight ownership or mode is unsafe'
    [ "$(stat -c '%u:%g:%a' "$MANIFEST_TEMP" 2>/dev/null || true)" = '0:0:600' ] \
        || die 'staged root cleaner manifest ownership or mode is unsafe'
    mv -f -- "$CLEANER_INSTALL_TEMP" "$CLEANER_PATH" || die 'cannot publish verified root cleaner'
    CLEANER_INSTALL_TEMP=''
    mv -f -- "$PREFLIGHT_INSTALL_TEMP" "$PREFLIGHT_PATH" || die 'cannot publish verified clean-remove preflight'
    PREFLIGHT_INSTALL_TEMP=''
    mv -f -- "$MANIFEST_TEMP" "$MANIFEST_PATH" || die 'cannot publish root cleaner manifest'
    MANIFEST_TEMP=''
    [ "$(stat -c '%u:%g:%a' "$CLEANER_PATH" 2>/dev/null || true)" = '0:0:700' ] \
        || die 'published root cleaner ownership or mode is unsafe'
    [ "$(stat -c '%u:%g:%a' "$PREFLIGHT_PATH" 2>/dev/null || true)" = '0:0:755' ] \
        || die 'published clean-remove preflight ownership or mode is unsafe'
    [ "$(stat -c '%u:%g:%a' "$MANIFEST_PATH" 2>/dev/null || true)" = '0:0:600' ] \
        || die 'published root cleaner manifest ownership or mode is unsafe'
    [ "$(sha256sum "$CLEANER_PATH" | awk '{print $1}')" = "$CLEANER_SHA256" ] \
        || die 'published root cleaner hash does not match'
    [ "$(sha256sum "$PREFLIGHT_PATH" | awk '{print $1}')" = "$PREFLIGHT_SHA256" ] \
        || die 'published clean-remove preflight hash does not match'
}

[ "$(id -u)" -eq 0 ] || die 'must be executed as root'
[ "$#" -eq 8 ] || usage
[ "$1" = --install-user ] && [ "$3" = --candidate-sha ] && [ "$5" = --cleaner-sha256 ] && [ "$7" = --preflight-sha256 ] || usage
INSTALL_USER=$2
CANDIDATE_SHA=$4
CLEANER_SHA256=$6
PREFLIGHT_SHA256=$8
case "$INSTALL_USER" in ''|root|*[!A-Za-z0-9_-]*) die 'install user is invalid' ;; esac
is_sha40 "$CANDIDATE_SHA" || die 'candidate SHA must be exactly 40 lowercase hexadecimal characters'
is_sha256 "$CLEANER_SHA256" || die 'cleaner SHA-256 must be exactly 64 lowercase hexadecimal characters'
is_sha256 "$PREFLIGHT_SHA256" || die 'preflight SHA-256 must be exactly 64 lowercase hexadecimal characters'
for command_name in awk getent git install mktemp mv readlink rm sha256sum sh stat; do
    command -v "$command_name" >/dev/null 2>&1 || die "required command is unavailable: $command_name"
done

trap cleanup EXIT HUP INT TERM
require_candidate_repository
extract_verified_cleaner
publish_cleaner
printf '%s\n' 'status=success phase=clean-remove-provision'
