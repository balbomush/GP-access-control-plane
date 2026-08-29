#!/usr/bin/env bash
# Internal Pi validation transport only. Public installation uses bootstrap-linux.sh and an annotated tag.
set -Eeuo pipefail
REPO_URL='https://github.com/balbomush/GP-access-control-plane.git'
CANDIDATE_SHA=
INSTALL_WEB="${GP_INSTALL_WEB:-on}"
fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
usage() { printf '%s\n' 'usage: hardware-candidate-bootstrap.sh --candidate-sha <40-lowercase-hex>' >&2; exit 64; }
need() { command -v "$1" >/dev/null 2>&1 || fail "required command is unavailable: $1"; }
[ "$(id -u)" -ne 0 ] || fail 'run the internal hardware bootstrap as the GP install user, not root'
while [ "$#" -gt 0 ]; do
  case "$1" in
    --candidate-sha) [ "$#" -ge 2 ] || usage; CANDIDATE_SHA="$2"; shift 2 ;;
    *) usage ;;
  esac
done
printf '%s\n' "$CANDIDATE_SHA" | grep -Eq '^[0-9a-f]{40}$' || fail 'candidate SHA must be a full lowercase commit SHA'
case "$INSTALL_WEB" in on|off) ;; *) fail 'GP_INSTALL_WEB must be on or off' ;; esac
need git; need python3; need sudo
legacy_state="$HOME/gp/GP-access-control-plane/build/state"
source_dir="$(mktemp -d "${TMPDIR:-/tmp}/gp-hardware-candidate.XXXXXX")"
cleanup() { rm -rf -- "$source_dir"; }
trap cleanup EXIT
git clone --no-checkout --depth=1 --branch dev "$REPO_URL" "$source_dir"
git -C "$source_dir" checkout --detach "$CANDIDATE_SHA"
[ "$(git -C "$source_dir" rev-parse HEAD)" = "$CANDIDATE_SHA" ] || fail 'checkout does not match the exact candidate SHA'
[ "$(git -C "$source_dir" rev-parse refs/remotes/origin/dev)" = "$CANDIDATE_SHA" ] || fail 'candidate SHA is not the frozen origin/dev tip'
[ -z "$(git -C "$source_dir" status --porcelain)" ] || fail 'exact candidate source tree is not clean'
initial_install=off
if python3 "$source_dir/scripts/clean-install-vault.py" --verify --state-dir "$legacy_state" --home "$HOME"; then
  :
elif [ -d "$legacy_state" ] && [ ! -L "$legacy_state" ]; then
  python3 "$source_dir/scripts/clean-install-vault.py" --state-dir "$legacy_state" --home "$HOME"
  python3 "$source_dir/scripts/clean-install-vault.py" --verify --state-dir "$legacy_state" --home "$HOME"
else
  initial_install=on
fi
sudo -n -- "$source_dir/scripts/install-linux.sh" --source-dir "$source_dir" --install-user "$(id -un)" --candidate-sha "$CANDIDATE_SHA" --web "$INSTALL_WEB" --initial-install "$initial_install"
