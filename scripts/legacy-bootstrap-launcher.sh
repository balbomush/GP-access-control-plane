#!/bin/sh
# Unprivileged entrypoint for the one-time legacy transition. The payload is
# never passed directly to a root shell from the working tree.
set -eu
umask 077

readonly TRUSTED_PATH='/usr/sbin:/usr/bin:/sbin:/bin'
readonly SUDO='/usr/bin/sudo'
readonly ENV='/usr/bin/env'
readonly INSTALL='/usr/bin/install'
readonly SHA256SUM='/usr/bin/sha256sum'
readonly AWK='/usr/bin/awk'
readonly STAT='/usr/bin/stat'
readonly READLINK='/usr/bin/readlink'
readonly TEST='/usr/bin/test'
readonly SH='/bin/sh'
readonly RM='/usr/bin/rm'
readonly RMDIR='/usr/bin/rmdir'
readonly STAGE_ROOT='/var/lib/gp-control-plane/legacy-bootstrap/payloads'

PATH="$TRUSTED_PATH"
export PATH

usage() {
  printf '%s\n' 'Usage: legacy-bootstrap-launcher.sh --bootstrap-sha SHA256 --candidate-ref refs/heads/dev --candidate-sha SHA40' >&2
}

fail() {
  printf '%s\n' "legacy-bootstrap-launcher: $1" >&2
  exit "${2:-1}"
}

is_sha256() {
  case "${1:-}" in ''|*[!0-9a-f]*) return 1 ;; esac
  [ "${#1}" -eq 64 ]
}

is_commit_sha() {
  case "${1:-}" in ''|*[!0-9a-f]*) return 1 ;; esac
  [ "${#1}" -eq 40 ]
}

require_trusted_utility() {
  [ -x "$1" ] || fail "trusted utility is unavailable: $1"
}

trusted_root() {
  "$SUDO" "$ENV" -i "PATH=$TRUSTED_PATH" "$@"
}

root_directory_is_trusted() {
  directory="$1"
  canonical="$(trusted_root "$READLINK" -f -- "$directory")" \
    || fail "cannot resolve staging directory: $directory"
  [ "$canonical" = "$directory" ] || fail "staging directory is not canonical: $directory"
  metadata="$(trusted_root "$STAT" -c '%u:%a:%F' -- "$directory")" \
    || fail "cannot inspect staging directory: $directory"
  owner="${metadata%%:*}"
  remainder="${metadata#*:}"
  mode="${remainder%%:*}"
  file_type="${remainder#*:}"
  [ "$owner" = 0 ] && [ "$file_type" = directory ] \
    || fail "staging directory is not root-owned: $directory"
  case "$mode" in
    [0-7][0145][0145]) ;;
    *) fail "staging directory is writable by a non-root user: $directory" ;;
  esac
}

ensure_stage_directory() {
  root_directory_is_trusted /
  root_directory_is_trusted /var
  root_directory_is_trusted /var/lib

  stage_parent=/var/lib/gp-control-plane
  trusted_root "$INSTALL" -d -m 0700 -o root -g root -- "$stage_parent"
  root_directory_is_trusted "$stage_parent"

  stage_parent=/var/lib/gp-control-plane/legacy-bootstrap
  trusted_root "$INSTALL" -d -m 0700 -o root -g root -- "$stage_parent"
  root_directory_is_trusted "$stage_parent"

  trusted_root "$INSTALL" -d -m 0700 -o root -g root -- "$STAGE_ROOT"
  root_directory_is_trusted "$STAGE_ROOT"
}

verify_source_payload() {
  [ -f "$PAYLOAD" ] && [ ! -L "$PAYLOAD" ] \
    || fail 'legacy bootstrap payload must be a regular non-symlink file'
  actual_source_sha="$("$SHA256SUM" -- "$PAYLOAD" | "$AWK" '{print $1}')"
  [ "$actual_source_sha" = "$BOOTSTRAP_SHA" ] \
    || fail 'bootstrap payload SHA256 does not match --bootstrap-sha'
}

stage_and_verify_payload() {
  stage_suffix="$$"
  case "$stage_suffix" in ''|*[!0-9]*) fail 'launcher process identifier is invalid' ;; esac
  STAGED_DIRECTORY="$STAGE_ROOT/payload-$BOOTSTRAP_SHA-$stage_suffix"
  STAGED_PAYLOAD="$STAGED_DIRECTORY/legacy-bootstrap.sh"

  ensure_stage_directory
  trusted_root "$TEST" ! -e "$STAGED_DIRECTORY" ! -L "$STAGED_DIRECTORY" \
    || fail 'root staging path already exists'
  trusted_root "$INSTALL" -d -m 0700 -o root -g root -- "$STAGED_DIRECTORY"
  [ "$(trusted_root "$STAT" -c '%u:%g:%a:%F' -- "$STAGED_DIRECTORY")" = '0:0:700:directory' ] \
    || fail 'root staging did not create a root-owned mode 0700 payload directory'
  trusted_root "$TEST" -f "$PAYLOAD" ! -L "$PAYLOAD" \
    || fail 'legacy bootstrap payload changed into an unsafe path before staging'
  trusted_root "$INSTALL" -T -m 0700 -o root -g root -- "$PAYLOAD" "$STAGED_PAYLOAD"
  trusted_root "$TEST" -f "$STAGED_PAYLOAD" ! -L "$STAGED_PAYLOAD" \
    || fail 'root staging did not create a regular payload file'
  [ "$(trusted_root "$STAT" -c '%u:%g:%a' -- "$STAGED_PAYLOAD")" = '0:0:700' ] \
    || fail 'root staging did not create a root-owned mode 0700 payload file'
  actual_staged_sha="$(trusted_root "$SHA256SUM" -- "$STAGED_PAYLOAD" | "$AWK" '{print $1}')"
  [ "$actual_staged_sha" = "$BOOTSTRAP_SHA" ] \
    || fail 'root staged payload SHA256 does not match --bootstrap-sha'
}

cleanup_staged_payload() {
  payload_status="$1"
  trap - 0
  [ -n "${STAGED_DIRECTORY:-}" ] && [ -n "${STAGED_PAYLOAD:-}" ] || exit "$payload_status"

  stage_suffix="${STAGED_DIRECTORY#"$STAGE_ROOT/payload-$BOOTSTRAP_SHA-"}"
  case "$stage_suffix" in ''|*[!0-9]*) exit "$payload_status" ;; esac
  [ "$STAGED_DIRECTORY" = "$STAGE_ROOT/payload-$BOOTSTRAP_SHA-$stage_suffix" ] \
    && [ "$STAGED_PAYLOAD" = "$STAGED_DIRECTORY/legacy-bootstrap.sh" ] \
    && trusted_root "$TEST" -d "$STAGED_DIRECTORY" ! -L "$STAGED_DIRECTORY" \
    && [ "$(trusted_root "$STAT" -c '%u:%g:%a:%F' -- "$STAGED_DIRECTORY")" = '0:0:700:directory' ] \
    || exit "$payload_status"

  if ! trusted_root "$RM" -f -- "$STAGED_PAYLOAD" || ! trusted_root "$RMDIR" -- "$STAGED_DIRECTORY"; then
    printf '%s\n' 'legacy-bootstrap-launcher: cannot remove root staged payload' >&2
    exit 1
  fi
  exit "$payload_status"
}

[ "$#" -eq 6 ] || { usage; exit 2; }
[ "$1" = --bootstrap-sha ] && [ "$3" = --candidate-ref ] && [ "$5" = --candidate-sha ] || { usage; exit 2; }
BOOTSTRAP_SHA="$2"
CANDIDATE_REF="$4"
CANDIDATE_SHA="$6"
is_sha256 "$BOOTSTRAP_SHA" || fail 'bootstrap SHA256 must be exactly 64 lowercase hexadecimal characters' 2
[ "$CANDIDATE_REF" = refs/heads/dev ] || fail 'candidate ref must be refs/heads/dev' 2
is_commit_sha "$CANDIDATE_SHA" || fail 'candidate SHA must be exactly 40 lowercase hexadecimal characters' 2

for trusted_utility in "$SHA256SUM" "$AWK"; do
  require_trusted_utility "$trusted_utility"
done

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)"
PAYLOAD="$SCRIPT_DIR/legacy-bootstrap.sh"
verify_source_payload

for trusted_utility in "$SUDO" "$ENV" "$INSTALL" "$STAT" "$READLINK" "$TEST" "$SH" "$RM" "$RMDIR"; do
  require_trusted_utility "$trusted_utility"
done
trap 'cleanup_staged_payload $?' 0
stage_and_verify_payload

"$SUDO" "$ENV" -i "PATH=$TRUSTED_PATH" \
  "LEGACY_BOOTSTRAP_STAGED_PATH=$STAGED_PAYLOAD" \
  "LEGACY_BOOTSTRAP_STAGED_DIR=$STAGED_DIRECTORY" \
  "LEGACY_BOOTSTRAP_STAGED_SHA=$BOOTSTRAP_SHA" \
  "$SH" "$STAGED_PAYLOAD" \
  --bootstrap-sha "$BOOTSTRAP_SHA" \
  --candidate-ref "$CANDIDATE_REF" \
  --candidate-sha "$CANDIDATE_SHA"
