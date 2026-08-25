#!/usr/bin/env bash
set -Eeuo pipefail

if [ -n "${GP_INSTALL_CONFIG:-}" ]; then
  [ -r "$GP_INSTALL_CONFIG" ] || { printf '\nERROR: install config is not readable: %s\n' "$GP_INSTALL_CONFIG" >&2; exit 1; }
  # shellcheck disable=SC1090
  set -a
  . "$GP_INSTALL_CONFIG"
  set +a
fi

REPO_URL="${GP_REPO_URL:-https://github.com/balbomush/GP-access-control-plane.git}"
INSTALL_REF="${GP_BRANCH:-latest-stable}"

log() {
  printf '\n==> %s\n' "$1"
}

fail() {
  printf '\nERROR: %s\n' "$1" >&2
  exit 1
}

need_command() {
  command -v "$1" >/dev/null 2>&1 || fail "Command not found: $1"
}

as_root() {
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
  else
    sudo "$@"
  fi
}

resolve_install_ref() {
  case "$INSTALL_REF" in
    latest|stable|latest-stable)
      log "Resolving latest stable GP release tag"
      resolved_ref="$(git ls-remote --tags --refs "$REPO_URL" "v*" \
        | awk '{print $2}' \
        | sed 's#refs/tags/##' \
        | grep -E '^v[0-9]+([.][0-9]+)*$' \
        | sort -V \
        | tail -n 1 || true)"
      [ -n "$resolved_ref" ] || fail "Cannot resolve latest stable release tag from $REPO_URL. Set GP_BRANCH=<tag> explicitly."
      INSTALL_REF="$resolved_ref"
      ;;
  esac
}

validate_release_tag() {
  printf '%s\n' "$INSTALL_REF" | grep -Eq '^v[0-9]+\.[0-9]+\.[0-9]+$' \
    || fail "GP_BRANCH must be an immutable release tag vX.Y.Z"
}

if ! command -v apt-get >/dev/null 2>&1; then
  fail "This bootstrap supports Debian/Ubuntu-like systems with apt-get."
fi

if [ "$(id -u)" -ne 0 ]; then
  need_command sudo
fi

log "Installing bootstrap dependencies"
as_root apt-get update
as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y ca-certificates git

need_command git

resolve_install_ref
validate_release_tag
export GP_BRANCH="$INSTALL_REF"

checkout_dir="$(mktemp -d)"
trap 'rm -rf -- "$checkout_dir"' EXIT
log "Verifying annotated release tag $INSTALL_REF"
git clone --no-checkout --depth=1 --branch "$INSTALL_REF" "$REPO_URL" "$checkout_dir"
[ "$(git -C "$checkout_dir" cat-file -t "refs/tags/$INSTALL_REF" 2>/dev/null || true)" = tag ] \
  || fail "GP_BRANCH must resolve to an annotated immutable release tag: $INSTALL_REF"
git -C "$checkout_dir" checkout --detach "$INSTALL_REF"
tag_commit="$(git -C "$checkout_dir" rev-parse --verify "refs/tags/$INSTALL_REF^{commit}")"
head_commit="$(git -C "$checkout_dir" rev-parse --verify HEAD)"
[ "$head_commit" = "$tag_commit" ] || fail 'verified release checkout does not match the annotated tag commit'
installer="$checkout_dir/scripts/install-linux.sh"
[ -f "$installer" ] && [ ! -L "$installer" ] || fail 'verified release tag has no regular install-linux.sh'
log "Running verified GP installer from $INSTALL_REF"
bash "$installer" "$@"
