#!/bin/sh
# Fixed root-owned clean-remove adapter for the legacy one-way bridge.
#
# This file is a trust-anchor payload, not a provisioner to be run from a
# candidate checkout. Its first installation is an out-of-band physical
# root-console operation described in docs/clean-install-trust-anchor.md.
set -eu

PATH='/usr/sbin:/usr/bin:/sbin:/bin'
export PATH

readonly ROOT_ADAPTER='/usr/local/libexec/gp-control-plane/gp-clean-remove-adapter'
readonly ROOT_ADAPTER_MANIFEST='/usr/local/libexec/gp-control-plane/gp-clean-remove-adapter.manifest'
readonly ROOT_CLEANER='/usr/local/libexec/gp-control-plane/gp-clean-remove-root'
readonly ROOT_PREFLIGHT='/usr/local/libexec/gp-control-plane/gp-clean-remove-preflight'

die() { printf 'gp-clean-remove-adapter: %s\n' "$1" >&2; exit 126; }

is_sha40() {
    case "${1:-}" in ????????????????????????????????????????) ;; *) return 1 ;; esac
    case "$1" in *[!0-9a-f]*) return 1 ;; esac
}

is_sha256() {
    case "${1:-}" in ????????????????????????????????????????????????????????????????) ;; *) return 1 ;; esac
    case "$1" in *[!0-9a-f]*) return 1 ;; esac
}

read_manifest_value() {
    awk -F= -v wanted="$1" '$1 == wanted { count++; value=$2 } END { if (count != 1) exit 1; print value }' \
        "$ROOT_ADAPTER_MANIFEST" 2>/dev/null || true
}

require_fixed_trust_anchor() {
    self_path=$(readlink -f -- "$0" 2>/dev/null || true)
    [ "$self_path" = "$ROOT_ADAPTER" ] || die "must be installed and invoked from $ROOT_ADAPTER"
    [ -f "$ROOT_ADAPTER" ] && [ ! -L "$ROOT_ADAPTER" ] \
        && [ "$(stat -c '%u:%g:%a' "$ROOT_ADAPTER" 2>/dev/null || true)" = '0:0:700' ] \
        || die 'adapter must be a root:root mode 0700 regular file'
    [ -f "$ROOT_ADAPTER_MANIFEST" ] && [ ! -L "$ROOT_ADAPTER_MANIFEST" ] \
        && [ "$(stat -c '%u:%g:%a' "$ROOT_ADAPTER_MANIFEST" 2>/dev/null || true)" = '0:0:600' ] \
        || die 'adapter manifest must be a root:root mode 0600 regular file'
    [ -f "$ROOT_CLEANER" ] && [ ! -L "$ROOT_CLEANER" ] \
        && [ "$(stat -c '%u:%g:%a' "$ROOT_CLEANER" 2>/dev/null || true)" = '0:0:700' ] \
        || die 'cleaner must be a root:root mode 0700 regular file'
    [ -f "$ROOT_PREFLIGHT" ] && [ ! -L "$ROOT_PREFLIGHT" ] \
        && [ "$(stat -c '%u:%g:%a' "$ROOT_PREFLIGHT" 2>/dev/null || true)" = '0:0:755' ] \
        || die 'preflight must be a root:root mode 0755 regular file'

    manifest_install_user=$(read_manifest_value install_user)
    manifest_candidate_sha=$(read_manifest_value candidate_sha)
    manifest_adapter_sha256=$(read_manifest_value adapter_sha256)
    manifest_cleaner_sha256=$(read_manifest_value cleaner_sha256)
    manifest_preflight_sha256=$(read_manifest_value preflight_sha256)
    case "$manifest_install_user" in ''|root|*[!A-Za-z0-9_-]*) die 'adapter manifest install user is invalid' ;; esac
    is_sha40 "$manifest_candidate_sha" || die 'adapter manifest candidate SHA is invalid'
    is_sha256 "$manifest_adapter_sha256" || die 'adapter manifest adapter hash is invalid'
    is_sha256 "$manifest_cleaner_sha256" || die 'adapter manifest cleaner hash is invalid'
    is_sha256 "$manifest_preflight_sha256" || die 'adapter manifest preflight hash is invalid'
    awk \
        -v install_user="$manifest_install_user" -v candidate_sha="$manifest_candidate_sha" \
        -v adapter_sha256="$manifest_adapter_sha256" -v cleaner_sha256="$manifest_cleaner_sha256" \
        -v preflight_sha256="$manifest_preflight_sha256" \
        'NR == 1 { valid = ($0 == "install_user=" install_user); next }
         NR == 2 { valid = valid && ($0 == "candidate_sha=" candidate_sha); next }
         NR == 3 { valid = valid && ($0 == "adapter_sha256=" adapter_sha256); next }
         NR == 4 { valid = valid && ($0 == "cleaner_sha256=" cleaner_sha256); next }
         NR == 5 { valid = valid && ($0 == "preflight_sha256=" preflight_sha256); next }
         { valid = 0 }
         END { exit (NR == 5 && valid) ? 0 : 1 }' "$ROOT_ADAPTER_MANIFEST" >/dev/null 2>&1 \
        || die 'adapter manifest format is invalid'
    [ "$(sha256sum "$ROOT_ADAPTER" | awk '{print $1}')" = "$manifest_adapter_sha256" ] \
        || die 'adapter hash does not match its manifest'
    [ "$(sha256sum "$ROOT_CLEANER" | awk '{print $1}')" = "$manifest_cleaner_sha256" ] \
        || die 'cleaner hash does not match adapter manifest'
    [ "$(sha256sum "$ROOT_PREFLIGHT" | awk '{print $1}')" = "$manifest_preflight_sha256" ] \
        || die 'preflight hash does not match adapter manifest'
}

[ "$(id -u)" -eq 0 ] || die 'must be executed as root'
[ "$#" -eq 2 ] && [ "$1" = clean-remove ] && [ "$2" = --confirm-clean-remove ] || {
    printf '%s\n' 'usage: gp-clean-remove-adapter clean-remove --confirm-clean-remove' >&2
    exit 64
}
for command_name in awk id readlink sha256sum stat; do
    command -v "$command_name" >/dev/null 2>&1 || die "required command is unavailable: $command_name"
done
require_fixed_trust_anchor
exec "$ROOT_CLEANER" --install-user "$manifest_install_user" --confirm-clean-remove
