#!/usr/bin/env bash
# One user-visible clean-install command. Everything before sudo is safe to retry.
set -Eeuo pipefail
REPO_URL="${GP_REPO_URL:-https://github.com/balbomush/GP-access-control-plane.git}"
TAG="${GP_BRANCH:-}"
INSTALL_WEB="${GP_INSTALL_WEB:-on}"
fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || fail "required command is unavailable: $1"; }
[ "$(id -u)" -ne 0 ] || fail 'run the clean installer as the GP install user, not root'
[ -n "$TAG" ] || fail 'GP_BRANCH must name the exact annotated release tag, for example v0.4.0'
printf '%s\n' "$TAG" | grep -Eq '^v[0-9]+\.[0-9]+\.[0-9]+$' || fail 'GP_BRANCH must be an exact release tag vX.Y.Z'
case "$INSTALL_WEB" in on|off) ;; *) fail 'GP_INSTALL_WEB must be on or off' ;; esac
need git; need python3; need sudo
legacy_state="$HOME/gp/GP-access-control-plane/build/state"
source_dir="$(mktemp -d "${TMPDIR:-/tmp}/gp-clean-install.XXXXXX")"
cleanup() { rm -rf -- "$source_dir"; }
trap cleanup EXIT
git clone --no-checkout --depth=1 --branch "$TAG" "$REPO_URL" "$source_dir"
[ "$(git -C "$source_dir" cat-file -t "refs/tags/$TAG" 2>/dev/null || true)" = tag ] || fail 'release tag must be annotated'
git -C "$source_dir" checkout --detach "$TAG"
[ "$(git -C "$source_dir" rev-parse HEAD)" = "$(git -C "$source_dir" rev-parse "refs/tags/$TAG^{commit}")" ] || fail 'checkout does not match the annotated tag'
[ -z "$(git -C "$source_dir" status --porcelain)" ] || fail 'exact-tag source tree is not clean'
# A present canonical legacy path is never an initial install. Reject unsafe
# objects before the only sudo call, so the destructive phase cannot erase them.
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
    # The exact v0.4 tag creates the vault because immutable legacy tags cannot grow this API.
    python3 "$source_dir/scripts/clean-install-vault.py" --state-dir "$legacy_state" --home "$HOME"
    python3 "$source_dir/scripts/clean-install-vault.py" --verify --state-dir "$legacy_state" --home "$HOME"
  fi
elif python3 "$source_dir/scripts/clean-install-vault.py" --verify --state-dir "$legacy_state" --home "$HOME"; then
  :
else
  initial_install=on
fi
sudo -- "$source_dir/scripts/install-linux.sh" --source-dir "$source_dir" --install-user "$(id -un)" --tag "$TAG" --web "$INSTALL_WEB" --initial-install "$initial_install"
