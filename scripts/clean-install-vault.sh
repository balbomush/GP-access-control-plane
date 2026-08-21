#!/bin/sh
# Explicit unprivileged launcher for the device-local clean-install protocol.
# It never reads, moves or deletes the vault.  The installed root helper owns
# the fixed managed surface and validates its own bounded command grammar.
set -eu

PATH=/usr/bin:/bin
export PATH

CHECKOUT_USER=balbomush
ROOT_HELPER=/usr/local/libexec/gp-control-plane/gp-root-helper
CANDIDATE_REF=refs/heads/dev

die() { printf '%s\n' "$*" >&2; exit 64; }
usage() {
    printf '%s\n' \
        'usage: clean-install-vault.sh --vault-id 32-lowercase-hex --candidate-ref refs/heads/dev --expected-sha 40-lowercase-hex --confirm-clean-install' >&2
    exit 64
}

require_installed_helper() {
    [ -f "$ROOT_HELPER" ] && [ ! -L "$ROOT_HELPER" ] && [ -x "$ROOT_HELPER" ] ||
        die "Installed trusted clean-install helper is unavailable: $ROOT_HELPER"
}

validate_vault_id() {
    case "$1" in
        ???????????????????????????????? ) ;;
        *) die 'vault ID must be exactly 32 lowercase hexadecimal characters' ;;
    esac
    case "$1" in *[!0-9a-f]*) die 'vault ID must be exactly 32 lowercase hexadecimal characters' ;; esac
}

validate_expected_sha() {
    case "$1" in
        ???????????????????????????????????????? ) ;;
        *) die 'expected SHA must be exactly 40 lowercase hexadecimal characters' ;;
    esac
    case "$1" in *[!0-9a-f]*) die 'expected SHA must be exactly 40 lowercase hexadecimal characters' ;; esac
}

[ "$(id -u)" -ne 0 ] || die "Run this clean-install launcher as the unprivileged install user: $CHECKOUT_USER"
[ "$(id -un)" = "$CHECKOUT_USER" ] || die "Clean install must be started by install user: $CHECKOUT_USER"
[ "$#" -eq 7 ] || usage
[ "$1" = --vault-id ] && [ "$3" = --candidate-ref ] && [ "$5" = --expected-sha ] && [ "$7" = --confirm-clean-install ] || usage

VAULT_ID=$2
[ "$4" = "$CANDIDATE_REF" ] || die "candidate ref must be exactly $CANDIDATE_REF"
EXPECTED_SHA=$6
validate_vault_id "$VAULT_ID"
validate_expected_sha "$EXPECTED_SHA"
require_installed_helper

# The acknowledgement is deliberately local.  Do not forward a reusable
# confirmation or backend restore token to sudo/root.
exec /usr/bin/sudo -n "$ROOT_HELPER" clean-install \
    --vault-id "$VAULT_ID" \
    --candidate-ref "$CANDIDATE_REF" \
    --expected-sha "$EXPECTED_SHA" \
    --apply
