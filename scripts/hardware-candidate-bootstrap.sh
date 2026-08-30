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
if [ -e "$legacy_state" ] || [ -L "$legacy_state" ]; then
  [ -d "$legacy_state" ] && [ ! -L "$legacy_state" ] \
    || fail "canonical legacy state is not a non-symlink directory: $legacy_state"
  legacy_state_canonical="$(readlink -f -- "$legacy_state" 2>/dev/null || true)"
  [ "$legacy_state_canonical" = "$legacy_state" ] \
    || fail "canonical legacy state path is unsafe: $legacy_state"
  legacy_strategy_dir="$legacy_state/strategy-finder"
  [ -d "$legacy_strategy_dir" ] && [ ! -L "$legacy_strategy_dir" ] \
    || fail "canonical legacy strategy-finder is not a non-symlink directory: $legacy_strategy_dir"
  legacy_strategy_dir_canonical="$(readlink -f -- "$legacy_strategy_dir" 2>/dev/null || true)"
  [ "$legacy_strategy_dir_canonical" = "$legacy_state_canonical/strategy-finder" ] \
    || fail "canonical legacy strategy-finder path escapes state: $legacy_strategy_dir"
  legacy_sqlite="$legacy_strategy_dir/state.sqlite3"
  [ -f "$legacy_sqlite" ] && [ ! -L "$legacy_sqlite" ] \
    || fail "canonical legacy state has an invalid layout: $legacy_state"
  legacy_sqlite_canonical="$(readlink -f -- "$legacy_sqlite" 2>/dev/null || true)"
  [ "$legacy_sqlite_canonical" = "$legacy_strategy_dir_canonical/state.sqlite3" ] \
    || fail "canonical legacy state database path escapes state: $legacy_sqlite"
  if python3 "$source_dir/scripts/clean-install-vault.py" --verify --state-dir "$legacy_state" --home "$HOME"; then
    :
  else
    python3 "$source_dir/scripts/clean-install-vault.py" --state-dir "$legacy_state" --home "$HOME"
    python3 "$source_dir/scripts/clean-install-vault.py" --verify --state-dir "$legacy_state" --home "$HOME"
  fi
elif python3 "$source_dir/scripts/clean-install-vault.py" --verify --state-dir "$legacy_state" --home "$HOME"; then
  :
else
  initial_install=on
fi
sudo -n -- "$source_dir/scripts/install-linux.sh" --source-dir "$source_dir" --install-user "$(id -un)" --candidate-sha "$CANDIDATE_SHA" --web "$INSTALL_WEB" --initial-install "$initial_install"
