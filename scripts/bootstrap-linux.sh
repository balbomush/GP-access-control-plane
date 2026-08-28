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
# A valid pending vault always wins: retry must never attempt a second export.
initial_install=off
if python3 "$source_dir/scripts/clean-install-vault.py" --verify --state-dir "$legacy_state" --home "$HOME"; then
  :
elif [ -d "$legacy_state" ] && [ ! -L "$legacy_state" ]; then
  # The exact v0.4 tag creates the vault because immutable legacy tags cannot grow this API.
  python3 "$source_dir/scripts/clean-install-vault.py" --state-dir "$legacy_state" --home "$HOME"
  python3 "$source_dir/scripts/clean-install-vault.py" --verify --state-dir "$legacy_state" --home "$HOME"
else
  initial_install=on
fi
sudo -- "$source_dir/scripts/install-linux.sh" --source-dir "$source_dir" --install-user "$(id -un)" --tag "$TAG" --web "$INSTALL_WEB" --initial-install "$initial_install"
