#!/bin/sh
# One-time, unprivileged clean-install handoff for legacy GP installations.
# It is not an updater and never invokes sudo. A legacy checkout does not
# contain the clean-install helper, so this tool fetches a pinned candidate as
# the install user and creates its compatible device-local vault before the
# separately confirmed root clean-remove phase.
set -eu
umask 077

PATH=/usr/bin:/bin
export PATH

readonly CANONICAL_UPSTREAM='https://github.com/balbomush/GP-access-control-plane.git'

die() { printf '%s\n' "clean-handoff: $*" >&2; exit 64; }
usage() {
  printf '%s\n' \
    'usage: legacy-bootstrap.sh --state-dir ABSOLUTE_PATH --candidate-ref refs/heads/dev-or-refs/tags/vX.Y.Z --candidate-sha 40-lowercase-hex' >&2
  exit 64
}

is_sha() {
  case "${1:-}" in ????????????????????????????????????????) ;; *) return 1 ;; esac
  case "$1" in *[!0-9a-f]*) return 1 ;; esac
}

valid_candidate_ref() {
  case "${1:-}" in refs/heads/dev|refs/tags/v[0-9]*.[0-9]*.[0-9]*) ;; *) return 1 ;; esac
  case "$1" in *'//'|*'/./'*|*'/../'*|*/'.'|*/'..'|*'@{'*|*'..'*|*[!A-Za-z0-9._/-]*) return 1 ;; esac
  git check-ref-format "$1" >/dev/null 2>&1
}

canonical_directory() {
  [ -d "$1" ] && [ ! -L "$1" ] || return 1
  resolved="$(readlink -f -- "$1" 2>/dev/null || true)"
  [ -n "$resolved" ] && [ "$resolved" = "$1" ] || return 1
  printf '%s\n' "$resolved"
}

require_unprivileged_user() {
  [ "$(id -u)" -ne 0 ] || die 'run the clean handoff as the legacy install user, never as root'
  [ -n "${HOME:-}" ] || die 'HOME is required'
  HOME="$(canonical_directory "$HOME")" || die 'HOME must be a canonical directory'
  export HOME
}

prepare_candidate_checkout() {
  handoff_root="$HOME/.cache/gp-control-plane/clean-handoff"
  install -d -m 0700 "$handoff_root"
  STAGE="$(mktemp -d "$handoff_root/candidate.XXXXXX")" || die 'cannot create private candidate stage'
  trap 'rm -rf -- "$STAGE"' EXIT HUP INT TERM
  git -C "$STAGE" init -q
  git -C "$STAGE" remote add origin "$CANONICAL_UPSTREAM"
  GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_TERMINAL_PROMPT=0 \
    git -C "$STAGE" fetch --no-tags --depth=1 origin "$CANDIDATE_REF" >/dev/null || die 'cannot fetch pinned clean-install candidate'
  case "$CANDIDATE_REF" in
    refs/tags/*)
      [ "$(git -C "$STAGE" cat-file -t FETCH_HEAD 2>/dev/null || true)" = tag ] \
        || die 'candidate release ref must resolve to an annotated immutable tag'
      ;;
  esac
  fetched_sha="$(git -C "$STAGE" rev-parse --verify 'FETCH_HEAD^{commit}' 2>/dev/null || true)"
  [ "$fetched_sha" = "$CANDIDATE_SHA" ] || die 'candidate SHA does not match the fetched ref'
  git -C "$STAGE" checkout --detach --force "$CANDIDATE_SHA" >/dev/null
  [ "$(git -C "$STAGE" rev-parse --verify HEAD)" = "$CANDIDATE_SHA" ] || die 'candidate checkout SHA does not match'
  [ -f "$STAGE/src/gp_control_plane/backups.py" ] || die 'candidate has no clean-install vault implementation'
}

[ "$#" -eq 6 ] || usage
[ "$1" = --state-dir ] && [ "$3" = --candidate-ref ] && [ "$5" = --candidate-sha ] || usage
STATE_DIR="$2"
CANDIDATE_REF="$4"
CANDIDATE_SHA="$6"
[ "${STATE_DIR#/}" != "$STATE_DIR" ] || die 'state dir must be absolute'
is_sha "$CANDIDATE_SHA" || die 'candidate SHA must be exactly 40 lowercase hexadecimal characters'
valid_candidate_ref "$CANDIDATE_REF" || die 'candidate ref must be refs/heads/dev or an immutable refs/tags/vX.Y.Z tag'
require_unprivileged_user
STATE_DIR="$(canonical_directory "$STATE_DIR")" || die 'state dir must be an existing canonical directory'
for command_name in git install mktemp python3 readlink rm; do
  command -v "$command_name" >/dev/null 2>&1 || die "required command is unavailable: $command_name"
done

prepare_candidate_checkout
PYTHONPATH="$STAGE/src" python3 - "$STATE_DIR" "$HOME" "$CANDIDATE_REF" "$CANDIDATE_SHA" <<'PY'
import json
import sys
from pathlib import Path

from gp_control_plane.backups import clean_install_vault_info, create_clean_install_vault

state_dir = Path(sys.argv[1])
home = Path(sys.argv[2])
candidate_ref = sys.argv[3]
candidate_sha = sys.argv[4]
created = create_clean_install_vault(state_dir, target_home=home)
info = clean_install_vault_info(target_home=home)
if not info.get("exists") or not info.get("pending"):
    raise SystemExit("created vault did not pass canonical pending validation")
if info.get("vault_id") != created.get("vault_id"):
    raise SystemExit("created vault id does not match canonical vault")
if info.get("archive_sha256") != created.get("archive_sha256"):
    raise SystemExit("created vault archive checksum does not match canonical vault")
print(json.dumps({
    "handoff": "ready",
    "candidate_ref": candidate_ref,
    "candidate_sha": candidate_sha,
    "vault_id": created["vault_id"],
    "archive_sha256": created["archive_sha256"],
    "archive_size_bytes": created["archive_size_bytes"],
    "schema_version": created["schema_version"],
    "confirmation_token": created["confirmation_token"],
}, separators=(",", ":")))
PY
