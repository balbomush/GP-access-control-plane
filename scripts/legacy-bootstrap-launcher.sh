#!/bin/sh
# Compatibility entry point for v0.3.4 and v0.3.5-alpha.4 clean handoff.
# This wrapper stays in the user boundary: it locates its sibling payload and
# execs it without sudo, root staging, update, rollback, or service mutation.
set -eu
umask 077

PATH=/usr/bin:/bin
export PATH

die() { printf '%s\n' "clean-handoff-launcher: $*" >&2; exit 64; }

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)"
PAYLOAD="$SCRIPT_DIR/legacy-bootstrap.sh"
[ -f "$PAYLOAD" ] && [ ! -L "$PAYLOAD" ] && [ -r "$PAYLOAD" ] || die 'clean handoff payload is unavailable'
[ "$(id -u)" -ne 0 ] || die 'run the clean handoff as the legacy install user, never as root'

exec /bin/sh "$PAYLOAD" "$@"
