#!/usr/bin/env bash
set -Eeuo pipefail

REPO_URL="${GP_REPO_URL:-https://github.com/balbomush/GP-access-control-plane.git}"
BRANCH="${GP_BRANCH:-main}"
SERVICE_NAME="${GP_SERVICE_NAME:-gp-control-plane-web.service}"
WEB_HOST="${GP_WEB_HOST:-0.0.0.0}"
WEB_PORT="${GP_WEB_PORT:-8080}"
ZAPRET_REPO_URL="${ZAPRET_REPO_URL:-https://github.com/bol-van/zapret2.git}"
ZAPRET_BRANCH="${ZAPRET_BRANCH:-master}"
ZAPRET_DIR="${ZAPRET_DIR:-/opt/zapret2}"

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

CURRENT_UID="$(id -u)"
CURRENT_USER="$(id -un)"

if [ -n "${GP_INSTALL_USER:-}" ]; then
  TARGET_USER="$GP_INSTALL_USER"
elif [ "$CURRENT_UID" -eq 0 ] && [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != "root" ]; then
  TARGET_USER="$SUDO_USER"
else
  TARGET_USER="$CURRENT_USER"
fi

TARGET_ENTRY="$(getent passwd "$TARGET_USER" || true)"
[ -n "$TARGET_ENTRY" ] || fail "Cannot find user: $TARGET_USER"
TARGET_HOME="$(printf '%s\n' "$TARGET_ENTRY" | cut -d: -f6)"
[ -n "$TARGET_HOME" ] || fail "Cannot find home directory for user: $TARGET_USER"
TARGET_GROUP="$(id -gn "$TARGET_USER" 2>/dev/null || true)"
[ -n "$TARGET_GROUP" ] || fail "Cannot find primary group for user: $TARGET_USER"
INSTALL_DIR="${GP_INSTALL_DIR:-$TARGET_HOME/gp/GP-access-control-plane}"
TARGET_BIN_DIR="$TARGET_HOME/.local/bin"

as_root() {
  if [ "$CURRENT_UID" -eq 0 ]; then
    "$@"
  else
    sudo "$@"
  fi
}

run_zapret_install_bin() {
  if [ "$CURRENT_UID" -eq 0 ]; then
    (cd "$ZAPRET_DIR" && ./install_bin.sh)
  else
    sudo sh -c 'cd "$1" && ./install_bin.sh' sh "$ZAPRET_DIR"
  fi
}

run_as_target() {
  if [ "$CURRENT_USER" = "$TARGET_USER" ]; then
    HOME="$TARGET_HOME" "$@"
  else
    sudo -H -u "$TARGET_USER" env HOME="$TARGET_HOME" "$@"
  fi
}

if [ "$CURRENT_UID" -ne 0 ]; then
  need_command sudo
fi
need_command bash

if ! command -v apt-get >/dev/null 2>&1; then
  fail "This installer supports Debian/Raspberry Pi OS systems with apt-get."
fi

log "Checking administrator access"
as_root true

log "Installing for user: $TARGET_USER"
log "Install directory: $INSTALL_DIR"

log "Updating system packages"
as_root apt-get update
as_root env DEBIAN_FRONTEND=noninteractive apt-get -y upgrade

log "Installing required packages"
as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y \
  bsdextrautils \
  ca-certificates \
  curl \
  dnsutils \
  git \
  iproute2 \
  ipset \
  iptables \
  nftables \
  python3 \
  python3-pip \
  python3-venv \
  sudo

log "Installing zapret2"
if [ -d "$ZAPRET_DIR/.git" ]; then
  if [ -n "$(as_root git -C "$ZAPRET_DIR" status --short)" ]; then
    log "zapret2 already exists and has local changes; keeping existing files"
  else
    as_root git -C "$ZAPRET_DIR" fetch origin "$ZAPRET_BRANCH"
    as_root git -C "$ZAPRET_DIR" checkout "$ZAPRET_BRANCH"
    as_root git -C "$ZAPRET_DIR" pull --ff-only origin "$ZAPRET_BRANCH"
  fi
elif [ -e "$ZAPRET_DIR" ]; then
  fail "zapret2 install path exists but is not a git repository: $ZAPRET_DIR"
else
  as_root mkdir -p "$(dirname "$ZAPRET_DIR")"
  as_root git clone --branch "$ZAPRET_BRANCH" "$ZAPRET_REPO_URL" "$ZAPRET_DIR"
fi

as_root chmod +x "$ZAPRET_DIR/blockcheck2.sh" "$ZAPRET_DIR/install_bin.sh" 2>/dev/null || true
if ! run_zapret_install_bin; then
  log "zapret2 ready binaries were not found; installing build dependencies and compiling"
  as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y \
    build-essential \
    gcc \
    libcap-dev \
    libmnl-dev \
    libnetfilter-queue-dev \
    libsystemd-dev \
    make \
    zlib1g-dev
  as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y libluajit2-5.1-dev \
    || as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y libluajit-5.1-dev \
    || true
  as_root make -C "$ZAPRET_DIR" systemd || as_root make -C "$ZAPRET_DIR"
  run_zapret_install_bin
fi

[ -x "$ZAPRET_DIR/blockcheck2.sh" ] || fail "zapret2 blockcheck2.sh was not installed"
[ -x "$ZAPRET_DIR/nfq2/nfqws2" ] || fail "zapret2 nfqws2 was not installed"

log "Preparing zapret2 command wrappers"
as_root install -d -o "$TARGET_USER" -g "$TARGET_GROUP" "$TARGET_BIN_DIR"
TMP_BLOCKCHECK="$(mktemp)"
TMP_NFQWS="$(mktemp)"
cat > "$TMP_BLOCKCHECK" <<WRAPPER
#!/bin/sh
exec "$ZAPRET_DIR/blockcheck2.sh" "\$@"
WRAPPER
cat > "$TMP_NFQWS" <<WRAPPER
#!/bin/sh
exec "$ZAPRET_DIR/nfq2/nfqws2" "\$@"
WRAPPER
as_root install -m 0755 -o "$TARGET_USER" -g "$TARGET_GROUP" "$TMP_BLOCKCHECK" "$TARGET_BIN_DIR/blockcheck2.sh"
as_root install -m 0755 -o "$TARGET_USER" -g "$TARGET_GROUP" "$TMP_NFQWS" "$TARGET_BIN_DIR/nfqws2"
rm -f "$TMP_BLOCKCHECK" "$TMP_NFQWS"

run_as_target sh -c 'if ! grep -qs '\''export PATH="$HOME/.local/bin:$PATH"'\'' "$HOME/.profile"; then printf '\''\nexport PATH="$HOME/.local/bin:$PATH"\n'\'' >> "$HOME/.profile"; fi'
export PATH="$TARGET_BIN_DIR:$PATH"

log "Installing GP Access Control Plane"
run_as_target mkdir -p "$(dirname "$INSTALL_DIR")"
if [ -d "$INSTALL_DIR/.git" ]; then
  if [ -n "$(run_as_target git -C "$INSTALL_DIR" status --short)" ]; then
    fail "Repository has local changes: $INSTALL_DIR. Commit or remove them, then run installer again."
  fi
  run_as_target git -C "$INSTALL_DIR" fetch origin "$BRANCH"
  run_as_target git -C "$INSTALL_DIR" checkout "$BRANCH"
  run_as_target git -C "$INSTALL_DIR" pull --ff-only origin "$BRANCH"
elif [ -e "$INSTALL_DIR" ]; then
  fail "Install path exists but is not a git repository: $INSTALL_DIR"
else
  run_as_target git clone --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
fi

log "Creating Python virtual environment"
run_as_target python3 -m venv "$INSTALL_DIR/.venv"
run_as_target "$INSTALL_DIR/.venv/bin/python" -m pip install --upgrade pip setuptools wheel
run_as_target "$INSTALL_DIR/.venv/bin/python" -m pip install -e "$INSTALL_DIR"

log "Creating systemd service"
as_root tee "/etc/systemd/system/$SERVICE_NAME" >/dev/null <<SERVICE
[Unit]
Description=GP Strategy Finder Web UI
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$TARGET_USER
WorkingDirectory=$INSTALL_DIR
Environment=HOME=$TARGET_HOME
Environment=PATH=$INSTALL_DIR/.venv/bin:$TARGET_BIN_DIR:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
ExecStart=$INSTALL_DIR/.venv/bin/gp-control-plane web --config $INSTALL_DIR/configs/orchestrator.example.yaml --host $WEB_HOST --port $WEB_PORT
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SERVICE

log "Starting service"
as_root systemctl daemon-reload
as_root systemctl enable "$SERVICE_NAME"
as_root systemctl restart "$SERVICE_NAME"

log "Checking installation"
run_as_target "$INSTALL_DIR/.venv/bin/gp-control-plane" zapret2 check-install --config "$INSTALL_DIR/configs/orchestrator.example.yaml" || true
as_root systemctl --no-pager --full status "$SERVICE_NAME" || true

IP_ADDRESS="$(hostname -I 2>/dev/null | awk '{print $1}')"
if [ -n "${IP_ADDRESS:-}" ]; then
  printf '\nDone. Open: http://%s:%s/\n' "$IP_ADDRESS" "$WEB_PORT"
else
  printf '\nDone. Open Raspberry Pi address on port %s.\n' "$WEB_PORT"
fi
