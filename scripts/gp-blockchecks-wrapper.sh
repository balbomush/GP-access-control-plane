#!/bin/sh
# Allowlisted GP root-helper target for blockcheckS. Prefer running `bs` as the
# install user; this wrapper exists so helper validate_run_target can permit it.
set -eu
PATH='/usr/sbin:/usr/bin:/sbin:/bin'
CONFIG_FILE="${GP_ROOT_HELPER_CONFIG:-/etc/default/gp-control-plane-root-helper}"
[ -r "$CONFIG_FILE" ] && . "$CONFIG_FILE"
BS="${BLOCKCHECKS_BS:-}"
[ -n "$BS" ] || {
  printf 'gp-blockchecks-wrapper: BLOCKCHECKS_BS is not set\n' >&2
  exit 126
}
[ -x "$BS" ] || {
  printf 'gp-blockchecks-wrapper: not executable: %s\n' "$BS" >&2
  exit 126
}
exec "$BS" "$@"
