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
RAW_BASE_URL="${GP_RAW_BASE_URL:-https://github.com/balbomush/GP-access-control-plane/raw}"
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

if ! command -v apt-get >/dev/null 2>&1; then
  fail "This bootstrap supports Debian/Ubuntu-like systems with apt-get."
fi

if [ "$(id -u)" -ne 0 ]; then
  need_command sudo
fi

log "Installing bootstrap dependencies"
as_root apt-get update
as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y ca-certificates curl git

need_command curl
need_command git

resolve_install_ref
export GP_BRANCH="$INSTALL_REF"

installer_url="$RAW_BASE_URL/$INSTALL_REF/scripts/install-linux.sh"
legacy_installer_url="$RAW_BASE_URL/$INSTALL_REF/scripts/install-raspberry-pi.sh"
log "Running GP installer from $INSTALL_REF"
tmp_installer="$(mktemp)"
trap 'rm -f "$tmp_installer"' EXIT
if ! curl -LfsS "$installer_url" -o "$tmp_installer"; then
  log "Generic installer was not found in $INSTALL_REF; falling back to legacy installer name"
  curl -LfsS "$legacy_installer_url" -o "$tmp_installer"
fi
bash "$tmp_installer" "$@"
