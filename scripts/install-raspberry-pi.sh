#!/usr/bin/env bash
set -Eeuo pipefail

REPO_URL="${GP_REPO_URL:-https://github.com/balbomush/GP-access-control-plane.git}"
BRANCH="${GP_BRANCH:-main}"
INSTALL_DIR="${GP_INSTALL_DIR:-$HOME/gp/GP-access-control-plane}"
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

if [ "$(id -u)" -eq 0 ]; then
  fail "Run this script as a normal user with sudo access, not as root."
fi

need_command sudo
need_command bash

if ! command -v apt-get >/dev/null 2>&1; then
  fail "This installer supports Debian/Raspberry Pi OS systems with apt-get."
fi

log "Checking sudo access"
sudo -v

log "Updating system packages"
sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get -y upgrade

log "Installing required packages"
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
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
  python3-venv

log "Installing zapret2"
if [ -d "$ZAPRET_DIR/.git" ]; then
  if [ -n "$(sudo git -C "$ZAPRET_DIR" status --short)" ]; then
    log "zapret2 already exists and has local changes; keeping existing files"
  else
    sudo git -C "$ZAPRET_DIR" fetch origin "$ZAPRET_BRANCH"
    sudo git -C "$ZAPRET_DIR" checkout "$ZAPRET_BRANCH"
    sudo git -C "$ZAPRET_DIR" pull --ff-only origin "$ZAPRET_BRANCH"
  fi
elif [ -e "$ZAPRET_DIR" ]; then
  fail "zapret2 install path exists but is not a git repository: $ZAPRET_DIR"
else
  sudo mkdir -p "$(dirname "$ZAPRET_DIR")"
  sudo git clone --branch "$ZAPRET_BRANCH" "$ZAPRET_REPO_URL" "$ZAPRET_DIR"
fi

sudo chmod +x "$ZAPRET_DIR/blockcheck2.sh" "$ZAPRET_DIR/install_bin.sh" 2>/dev/null || true
if ! sudo "$ZAPRET_DIR/install_bin.sh"; then
  log "zapret2 ready binaries were not found; installing build dependencies and compiling"
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
    build-essential \
    gcc \
    libcap-dev \
    libmnl-dev \
    libnetfilter-queue-dev \
    libsystemd-dev \
    make \
    zlib1g-dev
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y libluajit2-5.1-dev \
    || sudo DEBIAN_FRONTEND=noninteractive apt-get install -y libluajit-5.1-dev \
    || true
  sudo make -C "$ZAPRET_DIR" systemd || sudo make -C "$ZAPRET_DIR"
  sudo "$ZAPRET_DIR/install_bin.sh"
fi

[ -x "$ZAPRET_DIR/blockcheck2.sh" ] || fail "zapret2 blockcheck2.sh was not installed"
[ -x "$ZAPRET_DIR/nfq2/nfqws2" ] || fail "zapret2 nfqws2 was not installed"

log "Preparing zapret2 command wrappers"
mkdir -p "$HOME/.local/bin"
cat > "$HOME/.local/bin/blockcheck2.sh" <<WRAPPER
#!/bin/sh
exec "$ZAPRET_DIR/blockcheck2.sh" "\$@"
WRAPPER
cat > "$HOME/.local/bin/nfqws2" <<WRAPPER
#!/bin/sh
exec "$ZAPRET_DIR/nfq2/nfqws2" "\$@"
WRAPPER
chmod +x "$HOME/.local/bin/blockcheck2.sh" "$HOME/.local/bin/nfqws2"

case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *)
    if ! grep -qs 'export PATH="$HOME/.local/bin:$PATH"' "$HOME/.profile"; then
      printf '\nexport PATH="$HOME/.local/bin:$PATH"\n' >> "$HOME/.profile"
    fi
    export PATH="$HOME/.local/bin:$PATH"
    ;;
esac

log "Installing GP Access Control Plane"
mkdir -p "$(dirname "$INSTALL_DIR")"
if [ -d "$INSTALL_DIR/.git" ]; then
  if [ -n "$(git -C "$INSTALL_DIR" status --short)" ]; then
    fail "Repository has local changes: $INSTALL_DIR. Commit or remove them, then run installer again."
  fi
  git -C "$INSTALL_DIR" fetch origin "$BRANCH"
  git -C "$INSTALL_DIR" checkout "$BRANCH"
  git -C "$INSTALL_DIR" pull --ff-only origin "$BRANCH"
elif [ -e "$INSTALL_DIR" ]; then
  fail "Install path exists but is not a git repository: $INSTALL_DIR"
else
  git clone --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
fi

log "Creating Python virtual environment"
python3 -m venv "$INSTALL_DIR/.venv"
"$INSTALL_DIR/.venv/bin/python" -m pip install --upgrade pip setuptools wheel
"$INSTALL_DIR/.venv/bin/python" -m pip install -e "$INSTALL_DIR"

log "Creating systemd service"
sudo tee "/etc/systemd/system/$SERVICE_NAME" >/dev/null <<SERVICE
[Unit]
Description=GP Strategy Finder Web UI
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$INSTALL_DIR
Environment=PATH=$INSTALL_DIR/.venv/bin:$HOME/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
ExecStart=$INSTALL_DIR/.venv/bin/gp-control-plane web --config $INSTALL_DIR/configs/orchestrator.example.yaml --host $WEB_HOST --port $WEB_PORT
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SERVICE

log "Starting service"
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
sudo systemctl restart "$SERVICE_NAME"

log "Checking installation"
"$INSTALL_DIR/.venv/bin/gp-control-plane" zapret2 check-install --config "$INSTALL_DIR/configs/orchestrator.example.yaml" || true
sudo systemctl --no-pager --full status "$SERVICE_NAME" || true

IP_ADDRESS="$(hostname -I 2>/dev/null | awk '{print $1}')"
if [ -n "${IP_ADDRESS:-}" ]; then
  printf '\nDone. Open: http://%s:%s/\n' "$IP_ADDRESS" "$WEB_PORT"
else
  printf '\nDone. Open Raspberry Pi address on port %s.\n' "$WEB_PORT"
fi
