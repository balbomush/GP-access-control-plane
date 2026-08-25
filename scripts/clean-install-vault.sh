#!/bin/sh
# Explicit user-side acknowledgement for the clean-remove phase.
#
# Create the device-local vault in the application first. This launcher never
# receives a release ref, source path, archive, metadata or restore token; the
# installed root helper can only start the fixed root-owned cleaner.
set -eu

PATH=/usr/bin:/bin
export PATH

ROOT_HELPER=/usr/local/libexec/gp-control-plane/gp-root-helper

die() { printf '%s\n' "$*" >&2; exit 64; }

[ "$(id -u)" -ne 0 ] || die 'run clean-remove acknowledgement as the install user, never as root'
[ "$#" -eq 1 ] && [ "$1" = --confirm-clean-remove ] || {
    printf '%s\n' 'usage: clean-install-vault.sh --confirm-clean-remove' >&2
    exit 64
}
[ -f "$ROOT_HELPER" ] && [ ! -L "$ROOT_HELPER" ] && [ -x "$ROOT_HELPER" ] \
    || die "installed clean-remove helper is unavailable: $ROOT_HELPER"

exec /usr/bin/sudo -n "$ROOT_HELPER" clean-remove --confirm-clean-remove
