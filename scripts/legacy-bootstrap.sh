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
cleanup_stage() {
  [ -z "${RUNTIME_STAGE:-}" ] || rm -rf -- "$RUNTIME_STAGE"
  [ -z "${STAGE:-}" ] || rm -rf -- "$STAGE"
}
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
  case "${1:-}" in refs/heads/dev) git check-ref-format "$1" >/dev/null 2>&1; return ;; refs/tags/v*) ;; *) return 1 ;; esac
  case "$1" in *'//'|*'/./'*|*'/../'*|*/'.'|*/'..'|*'@{'*|*'..'*|*[!A-Za-z0-9._/-]*) return 1 ;; esac
  tag_version=${1#refs/tags/v}
  tag_major=${tag_version%%.*}
  tag_rest=${tag_version#*.}
  [ "$tag_rest" != "$tag_version" ] || return 1
  tag_minor=${tag_rest%%.*}
  tag_patch=${tag_rest#*.}
  [ "$tag_patch" != "$tag_rest" ] || return 1
  case "$tag_major:$tag_minor:$tag_patch" in *[!0-9:]*|:*|*::*|*:) return 1 ;; esac
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

require_exact_legacy_state_dir() {
  # The bridge is intentionally not a generic exporter.  The only supported
  # legacy source is the installation owned by this same install user.  Check
  # the supplied spelling before resolving it, so a symlink or an alternate
  # path that happens to resolve here cannot become an accepted input.
  expected="$HOME/gp/GP-access-control-plane/build/state"
  [ "$STATE_DIR" = "$expected" ] || die "state dir must be exactly $expected"
  canonical_directory "$expected" >/dev/null || die 'canonical legacy state directory is unavailable or unsafe'
}

prepare_candidate_checkout() {
  handoff_root="$HOME/.cache/gp-control-plane/clean-handoff"
  install -d -m 0700 "$handoff_root"
  CANDIDATE_REPOSITORY="$handoff_root/candidate-$CANDIDATE_SHA"
  if [ -e "$CANDIDATE_REPOSITORY" ] || [ -L "$CANDIDATE_REPOSITORY" ]; then
    validate_candidate_checkout "$CANDIDATE_REPOSITORY"
    return 0
  fi
  STAGE="$(mktemp -d "$handoff_root/candidate.XXXXXX")" || die 'cannot create private candidate stage'
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
  validate_candidate_checkout "$STAGE"
  mv -- "$STAGE" "$CANDIDATE_REPOSITORY" || die 'cannot publish pinned candidate repository'
  STAGE=''
}

validate_candidate_checkout() {
  candidate_path="$1"
  [ -d "$candidate_path" ] && [ ! -L "$candidate_path" ] \
    || die 'persisted candidate repository is unsafe'
  [ "$(readlink -f -- "$candidate_path" 2>/dev/null || true)" = "$candidate_path" ] \
    || die 'persisted candidate repository is non-canonical'
  GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null \
    git -C "$candidate_path" cat-file -e "$CANDIDATE_SHA^{commit}" 2>/dev/null \
    || die 'persisted candidate repository does not contain the requested commit'
  [ "$(GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null git -C "$candidate_path" rev-parse --verify 'HEAD^{commit}' 2>/dev/null || true)" = "$CANDIDATE_SHA" ] \
    || die 'persisted candidate repository does not match the requested SHA'
  if GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null git -C "$candidate_path" symbolic-ref -q HEAD >/dev/null 2>&1; then
    die 'persisted candidate repository HEAD must be detached'
  fi
  GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null \
    git -C "$candidate_path" diff --quiet --ignore-submodules --exit-code \
    || die 'persisted candidate repository has unstaged changes'
  GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null \
    git -C "$candidate_path" diff --cached --quiet --ignore-submodules --exit-code \
    || die 'persisted candidate repository has staged changes'
  candidate_status="$(GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null git -C "$candidate_path" status --porcelain=v1 --untracked-files=all 2>/dev/null)" \
    || die 'cannot inspect persisted candidate repository state'
  [ -z "$candidate_status" ] || die 'persisted candidate repository has local changes'
}

materialize_candidate_runtime() {
  RUNTIME_STAGE="$(mktemp -d "$handoff_root/runtime.XXXXXX")" || die 'cannot create private candidate runtime stage'
  runtime_archive="$RUNTIME_STAGE/candidate.tar"
  GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null \
    git -C "$CANDIDATE_REPOSITORY" archive --format=tar "$CANDIDATE_SHA" > "$runtime_archive" \
    || die 'cannot materialize pinned candidate runtime source'
  tar -xf "$runtime_archive" -C "$RUNTIME_STAGE" || die 'cannot extract pinned candidate runtime source'
  rm -f -- "$runtime_archive"
  [ -f "$RUNTIME_STAGE/src/gp_control_plane/backups.py" ] || die 'candidate has no clean-install vault implementation'
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
require_exact_legacy_state_dir
for command_name in git install mktemp python3 readlink rm mv tar; do
  command -v "$command_name" >/dev/null 2>&1 || die "required command is unavailable: $command_name"
done

trap cleanup_stage EXIT HUP INT TERM
prepare_candidate_checkout
materialize_candidate_runtime
PYTHONPATH="$RUNTIME_STAGE/src" python3 - "$STATE_DIR" "$HOME" "$CANDIDATE_REF" "$CANDIDATE_SHA" <<'PY'
import hashlib
import json
import os
import stat
import sys
from pathlib import Path

from gp_control_plane.backups import clean_install_vault_info, create_clean_install_vault_with_handoff_validation

state_dir = Path(sys.argv[1])
home = Path(sys.argv[2])
candidate_ref = sys.argv[3]
candidate_sha = sys.argv[4]
created = create_clean_install_vault_with_handoff_validation(state_dir, target_home=home)
info = clean_install_vault_info(target_home=home)
if not info.get("exists") or not info.get("pending"):
    raise SystemExit("created vault did not pass canonical pending validation")
if info.get("vault_id") != created.get("vault_id"):
    raise SystemExit("created vault id does not match canonical vault")
if info.get("archive_sha256") != created.get("archive_sha256"):
    raise SystemExit("created vault archive checksum does not match canonical vault")


def require_private_regular(path: Path, label: str) -> os.stat_result:
    details = path.lstat()
    if not stat.S_ISREG(details.st_mode):
        raise SystemExit(f"{label} must be a regular non-symlink file")
    if details.st_uid != os.geteuid():
        raise SystemExit(f"{label} owner does not match the install user")
    if stat.S_IMODE(details.st_mode) != 0o600:
        raise SystemExit(f"{label} must be mode 0600")
    return details


vault = home / ".local" / "share" / "gp-control-plane" / "clean-install-vault"
handoff = home / ".local" / "share" / "gp-control-plane" / "clean-install-handoff" / "handoff.json"
archive = vault / "archive.zip"
entry = vault / "entry.json"
for path, label in ((archive, "vault archive"), (entry, "vault entry"), (handoff, "device-local handoff")):
    require_private_regular(path, label)

entry_payload = json.loads(entry.read_text(encoding="utf-8"))
handoff_payload = json.loads(handoff.read_text(encoding="utf-8"))
vault_id = str(created["vault_id"])
archive_sha256 = str(created["archive_sha256"])
if str(entry_payload.get("vault_id") or "") != vault_id:
    raise SystemExit("vault entry id does not match the created vault")
if str(handoff_payload.get("vault_id") or "") != vault_id:
    raise SystemExit("device-local handoff id does not match the created vault")
if str(entry_payload.get("archive_sha256") or "") != archive_sha256:
    raise SystemExit("vault entry checksum does not match the created vault")
if hashlib.sha256(archive.read_bytes()).hexdigest() != archive_sha256:
    raise SystemExit("vault archive checksum does not match the created vault")
handoff_secret = str(handoff_payload.get("handoff_secret") or "")
if not handoff_secret:
    raise SystemExit("device-local handoff secret is absent")
if hashlib.sha256(handoff_secret.encode("utf-8")).hexdigest() != str(entry_payload.get("handoff_secret_sha256") or ""):
    raise SystemExit("device-local handoff secret does not match the vault entry")

print(json.dumps({
    "handoff": "ready",
    "candidate_ref": candidate_ref,
    "candidate_sha": candidate_sha,
    "vault_id": created["vault_id"],
    "archive_sha256": created["archive_sha256"],
    "archive_size_bytes": created["archive_size_bytes"],
    "schema_version": created["schema_version"],
}, separators=(",", ":")))
PY
