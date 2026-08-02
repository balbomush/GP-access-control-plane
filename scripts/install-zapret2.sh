#!/usr/bin/env bash
set -Eeuo pipefail

ZAPRET_REPO_URL="${ZAPRET_REPO_URL:-https://github.com/bol-van/zapret2.git}"
ZAPRET_BRANCH="${ZAPRET_BRANCH:-master}"
ZAPRET_DIR="${ZAPRET_DIR:-/opt/zapret2}"

fail() {
  printf '\nERROR: %s\n' "$1" >&2
  exit 1
}

as_root() {
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
  else
    sudo "$@"
  fi
}

if [ "$(id -u)" -ne 0 ] && ! command -v sudo >/dev/null 2>&1; then
  fail "Command not found: sudo"
fi

if ! command -v apt-get >/dev/null 2>&1; then
  fail "This installer supports Debian/Ubuntu-like systems with apt-get."
fi

as_root apt-get update
as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y git bsdextrautils

if [ -d "$ZAPRET_DIR/.git" ]; then
  as_root git -C "$ZAPRET_DIR" fetch origin "$ZAPRET_BRANCH"
  as_root git -C "$ZAPRET_DIR" checkout "$ZAPRET_BRANCH"
  as_root git -C "$ZAPRET_DIR" pull --ff-only origin "$ZAPRET_BRANCH"
elif [ -e "$ZAPRET_DIR" ]; then
  fail "zapret2 install path exists but is not a git repository: $ZAPRET_DIR"
else
  as_root mkdir -p "$(dirname "$ZAPRET_DIR")"
  as_root git clone --branch "$ZAPRET_BRANCH" "$ZAPRET_REPO_URL" "$ZAPRET_DIR"
fi

if [ "$(id -u)" -eq 0 ]; then
  (cd "$ZAPRET_DIR" && ./install_bin.sh)
else
  sudo sh -c 'cd "$1" && ./install_bin.sh' sh "$ZAPRET_DIR"
fi

[ -x "$ZAPRET_DIR/blockcheck2.sh" ] || fail "zapret2 blockcheck2.sh was not installed"
[ -x "$ZAPRET_DIR/nfq2/nfqws2" ] || fail "zapret2 nfqws2 was not installed"

printf '\nzapret2 installed in %s\n' "$ZAPRET_DIR"
