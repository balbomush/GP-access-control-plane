#!/usr/bin/env bash
set -Eeuo pipefail

INSTALL_FORCE_CLEAN="${GP_INSTALL_FORCE_CLEAN:-off}"
# Fixed, root-owned profile; a release update must not use a caller-selected path.
INSTALL_PROFILE="/etc/default/gp-control-plane-install-profile"
LEGACY_PROFILE_CAPTURE=off

release_update_enabled() {
  case "$INSTALL_FORCE_CLEAN" in
    1|true|TRUE|yes|YES|on|ON) return 0 ;;
    *) return 1 ;;
  esac
}

if release_update_enabled; then
  if [ -e "$INSTALL_PROFILE" ] || [ -L "$INSTALL_PROFILE" ]; then
    [ -f "$INSTALL_PROFILE" ] && [ ! -L "$INSTALL_PROFILE" ] || { printf '\nERROR: install profile is not a regular file: %s\n' "$INSTALL_PROFILE" >&2; exit 1; }
    profile_uid="$(stat -c '%u' "$INSTALL_PROFILE" 2>/dev/null || true)"
    profile_mode="$(stat -c '%a' "$INSTALL_PROFILE" 2>/dev/null || true)"
    [ "$profile_uid" = "0" ] && [ -n "$profile_mode" ] && [ $((8#$profile_mode & 022)) -eq 0 ] || { printf '\nERROR: install profile must be root-owned and not group/world-writable: %s\n' "$INSTALL_PROFILE" >&2; exit 1; }
    set -a
    # shellcheck disable=SC1090
    . "$INSTALL_PROFILE"
    set +a
  else
    LEGACY_PROFILE_CAPTURE=on
  fi
elif [ -n "${GP_INSTALL_CONFIG:-}" ]; then
  [ -r "$GP_INSTALL_CONFIG" ] || { printf '\nERROR: install config is not readable: %s\n' "$GP_INSTALL_CONFIG" >&2; exit 1; }
  # shellcheck disable=SC1090
  set -a
  . "$GP_INSTALL_CONFIG"
  set +a
fi

REPO_URL="${GP_REPO_URL:-https://github.com/balbomush/GP-access-control-plane.git}"
BRANCH="${GP_BRANCH:-latest-stable}"
SERVICE_NAME="${GP_SERVICE_NAME:-gp-control-plane-web.service}"
CORE_SERVICE_NAME="${GP_CORE_SERVICE_NAME:-gp-control-plane-core.service}"
INSTALL_WEB="${GP_INSTALL_WEB:-on}"
WEB_HOST="${GP_WEB_HOST:-0.0.0.0}"
WEB_PORT="${GP_WEB_PORT:-8080}"
WEB_ENV_FILE="${GP_WEB_ENV_FILE:-/etc/default/gp-control-plane-web}"
CORE_HOST="${GP_CORE_HOST:-127.0.0.1}"
CORE_PORT="${GP_CORE_PORT:-8081}"
CORE_URL="${GP_CORE_URL:-http://$CORE_HOST:$CORE_PORT}"
CORE_ENV_FILE="${GP_CORE_ENV_FILE:-/etc/default/gp-control-plane-core}"
ZAPRET_REPO_URL="${ZAPRET_REPO_URL:-https://github.com/bol-van/zapret2.git}"
ZAPRET_BRANCH="${ZAPRET_BRANCH:-master}"
ZAPRET_DIR="${ZAPRET_DIR:-/opt/zapret2}"
ROOT_HELPER_PATH="${GP_ROOT_HELPER_PATH:-/usr/local/libexec/gp-control-plane/gp-root-helper}"
ROOT_HELPER_CONFIG="${GP_ROOT_HELPER_CONFIG:-/etc/default/gp-control-plane-root-helper}"
ROOT_HELPER_RUN_DIR="${GP_ROOT_HELPER_RUN_DIR:-/run/gp-control-plane/runs}"
SUDOERS_PATH="${GP_SUDOERS_PATH:-/etc/sudoers.d/gp-control-plane-root-helper}"
SERVICE_MEMORY_HIGH="${GP_SERVICE_MEMORY_HIGH:-512M}"
SERVICE_MEMORY_MAX="${GP_SERVICE_MEMORY_MAX:-1G}"
REQUESTED_STEPS="${GP_INSTALL_STEPS:-all}"
INSTALL_FORCE_CLEAN="${GP_INSTALL_FORCE_CLEAN:-off}"

usage() {
  cat <<USAGE
Usage: install-linux.sh [--step STEP] [--steps a,b,c]

Default is --steps all. Available steps:
  packages,zapret,app,v2fly,root-helper,service,check

Runtime:
  GP_INSTALL_WEB=on   installs Core service and штатный Web UI proxy service.
  GP_INSTALL_WEB=off  installs only API-only Core service on GP_CORE_HOST:GP_CORE_PORT.
  GP_BRANCH=latest-stable installs the latest stable Git tag; this is the default.
  GP_BRANCH=<ref>         installs an explicit branch or tag, for example main.
USAGE
}

append_requested_step() {
  if [ "$REQUESTED_STEPS" = "all" ]; then
    REQUESTED_STEPS="$1"
  else
    REQUESTED_STEPS="$REQUESTED_STEPS,$1"
  fi
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --step)
      [ "$#" -ge 2 ] || { usage >&2; exit 2; }
      append_requested_step "$2"
      shift 2
      ;;
    --step=*)
      append_requested_step "${1#--step=}"
      shift
      ;;
    --steps)
      [ "$#" -ge 2 ] || { usage >&2; exit 2; }
      REQUESTED_STEPS="$2"
      shift 2
      ;;
    --steps=*)
      REQUESTED_STEPS="${1#--steps=}"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage >&2
      exit 2
      ;;
  esac
done

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

step_enabled() {
  [ "$REQUESTED_STEPS" = "all" ] && return 0
  case ",$REQUESTED_STEPS," in
    *",$1,"*) return 0 ;;
    *) return 1 ;;
  esac
}

step_log() {
  if step_enabled "$1"; then
    log "[$1] $2"
    return 0
  fi
  log "[$1] skipped"
  return 1
}

force_clean_enabled() {
  case "$INSTALL_FORCE_CLEAN" in
    1|true|TRUE|yes|YES|on|ON) return 0 ;;
    *) return 1 ;;
  esac
}

install_web_enabled() {
  case "$INSTALL_WEB" in
    1|true|TRUE|yes|YES|on|ON|web|WEB|ui|UI) return 0 ;;
    0|false|FALSE|no|NO|off|OFF|headless|HEADLESS|core|CORE) return 1 ;;
    *) fail "Unsupported GP_INSTALL_WEB value: $INSTALL_WEB" ;;
  esac
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
STATE_DIR="${GP_STATE_DIR:-$INSTALL_DIR/build/state}"
TARGET_BIN_DIR="$TARGET_HOME/.local/bin"
SERVICE_PATH="$INSTALL_DIR/.venv/bin:$TARGET_BIN_DIR:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

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
    HOME="$TARGET_HOME" PATH="$SERVICE_PATH" "$@"
  else
    sudo -H -u "$TARGET_USER" env HOME="$TARGET_HOME" PATH="$SERVICE_PATH" "$@"
  fi
}

repo_git() {
  run_as_target git -c safe.directory="$INSTALL_DIR" -C "$INSTALL_DIR" "$@"
}

resolve_install_ref() {
  case "$BRANCH" in
    latest|stable|latest-stable)
      log "Resolving latest stable GP release tag"
      resolved_ref="$(git ls-remote --tags --refs "$REPO_URL" "v*" \
        | awk '{print $2}' \
        | sed 's#refs/tags/##' \
        | grep -E '^v[0-9]+([.][0-9]+)*$' \
        | sort -V \
        | tail -n 1 || true)"
      [ -n "$resolved_ref" ] || fail "Cannot resolve latest stable release tag from $REPO_URL. Set GP_BRANCH=<tag> explicitly."
      BRANCH="$resolved_ref"
      log "Latest stable GP release: $BRANCH"
      ;;
  esac
}

apt_package_available() {
  apt-cache show "$1" >/dev/null 2>&1
}

install_luajit_dev_package() {
  for package in libluajit2-5.1-dev libluajit-5.1-dev; do
    if apt_package_available "$package"; then
      log "Installing LuaJIT build dependency: $package"
      as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y "$package"
      return 0
    fi
  done
  log "LuaJIT development package was not found; continuing because zapret2 can build without LuaJIT on some platforms"
}

install_service_env_file() {
  env_file="$1"
  if install_web_enabled; then
    install_web_value="on"
  else
    install_web_value="off"
  fi
  TMP_WEB_ENV="$(mktemp)"
  {
    install_dir_escaped="$(printf '%s' "$INSTALL_DIR" | sed "s/'/'\\\\''/g")"
    state_dir_escaped="$(printf '%s' "$STATE_DIR" | sed "s/'/'\\\\''/g")"
    printf "GP_INSTALL_DIR='%s'\n" "$install_dir_escaped"
    printf "GP_STATE_DIR='%s'\n" "$state_dir_escaped"
    printf "GP_INSTALL_WEB='%s'\n" "$install_web_value"
  } > "$TMP_WEB_ENV"
  as_root install -m 0640 -o root -g root "$TMP_WEB_ENV" "$env_file"
  rm -f "$TMP_WEB_ENV"
}

install_web_env_file() {
  install_service_env_file "$WEB_ENV_FILE"
}

install_systemd_service() {
  service_name="$1"
  description="$2"
  command_name="$3"
  host="$4"
  port="$5"
  env_file="$6"
  extra_args="${7:-}"
  after_extra="${8:-}"
  wants_extra="${9:-}"
  after_line="network-online.target"
  wants_line="network-online.target"
  [ -z "$after_extra" ] || after_line="$after_line $after_extra"
  [ -z "$wants_extra" ] || wants_line="$wants_line $wants_extra"
  exec_start="$INSTALL_DIR/.venv/bin/gp-control-plane $command_name --host $host --port $port"
  [ -z "$extra_args" ] || exec_start="$exec_start $extra_args"
  privileged_env=""
  if [ "$command_name" = "core" ]; then
    privileged_env="Environment=GP_ROOT_HELPER=$ROOT_HELPER_PATH
Environment=GP_ZAPRET_DIR=$ZAPRET_DIR"
  fi
  TMP_SERVICE="$(mktemp)"
  cat > "$TMP_SERVICE" <<SERVICE
[Unit]
Description=$description
After=$after_line
Wants=$wants_line

[Service]
Type=simple
User=$TARGET_USER
WorkingDirectory=$INSTALL_DIR
Environment=HOME=$TARGET_HOME
Environment=PATH=$SERVICE_PATH
$privileged_env
EnvironmentFile=-$env_file
ExecStart=$exec_start
MemoryAccounting=true
MemoryHigh=$SERVICE_MEMORY_HIGH
MemoryMax=$SERVICE_MEMORY_MAX
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SERVICE
  as_root install -m 0644 -o root -g root "$TMP_SERVICE" "/etc/systemd/system/$service_name"
  rm -f "$TMP_SERVICE"
}

prepare_v2fly_local_catalog() {
  run_as_target sh -c 'cd "$1" && GP_INSTALL_DIR="$1" GP_STATE_DIR="$2" "$1/.venv/bin/gp-control-plane" domain-sources prepare-v2fly' sh "$INSTALL_DIR" "$STATE_DIR"
}

if [ "$CURRENT_UID" -ne 0 ]; then
  need_command sudo
fi
need_command bash

if ! command -v apt-get >/dev/null 2>&1; then
  fail "This installer supports Debian/Ubuntu-like systems with apt-get."
fi

log "Checking administrator access"
as_root true

trusted_root_file() {
  profile_path="$1"
  [ -f "$profile_path" ] && [ ! -L "$profile_path" ] || return 1
  profile_uid="$(as_root stat -c '%u' "$profile_path" 2>/dev/null || true)"
  profile_mode="$(as_root stat -c '%a' "$profile_path" 2>/dev/null || true)"
  [ "$profile_uid" = "0" ] && [ -n "$profile_mode" ] && [ $((8#$profile_mode & 022)) -eq 0 ]
}

unit_value() {
  unit_file="/etc/systemd/system/$1"
  property="$2"
  trusted_root_file "$unit_file" || fail "Cannot safely read systemd unit: $unit_file"
  sed -n "s/^$property=//p" "$unit_file" | head -n 1
}

unit_option() {
  unit_file="/etc/systemd/system/$1"
  option="$2"
  trusted_root_file "$unit_file" || fail "Cannot safely read systemd unit: $unit_file"
  awk -v option="$option" '/^ExecStart=/ { for (i = 1; i < NF; i++) if ($i == option) { print $(i + 1); exit } }' "$unit_file"
}

load_trusted_service_env() {
  trusted_root_file "$1" || fail "Cannot safely read service environment: $1"
  set -a
  # shellcheck disable=SC1090
  . "$1"
  set +a
}

capture_legacy_install_profile() {
  [ -f "/etc/systemd/system/$CORE_SERVICE_NAME" ] || fail "Cannot capture legacy install profile: Core service is missing"
  CORE_ENV_FILE="$(unit_value "$CORE_SERVICE_NAME" EnvironmentFile | sed 's/^-//')"
  [ -n "$CORE_ENV_FILE" ] || fail "Cannot capture legacy install profile: Core EnvironmentFile is missing"
  load_trusted_service_env "$CORE_ENV_FILE"
  INSTALL_DIR="${GP_INSTALL_DIR:-$INSTALL_DIR}"
  STATE_DIR="${GP_STATE_DIR:-$STATE_DIR}"
  SERVICE_PATH="$INSTALL_DIR/.venv/bin:$TARGET_BIN_DIR:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
  CORE_HOST="$(unit_option "$CORE_SERVICE_NAME" --host)"
  CORE_PORT="$(unit_option "$CORE_SERVICE_NAME" --port)"
  [ -n "$CORE_HOST" ] && [ -n "$CORE_PORT" ] || fail "Cannot capture legacy install profile: Core host/port are missing"
  CORE_URL="http://$CORE_HOST:$CORE_PORT"
  if [ -f "/etc/systemd/system/$SERVICE_NAME" ]; then
    INSTALL_WEB=on
    WEB_ENV_FILE="$(unit_value "$SERVICE_NAME" EnvironmentFile | sed 's/^-//')"
    [ -n "$WEB_ENV_FILE" ] || fail "Cannot capture legacy install profile: Web EnvironmentFile is missing"
    load_trusted_service_env "$WEB_ENV_FILE"
    WEB_HOST="$(unit_option "$SERVICE_NAME" --host)"
    WEB_PORT="$(unit_option "$SERVICE_NAME" --port)"
    CORE_URL="$(unit_option "$SERVICE_NAME" --core-url)"
    [ -n "$WEB_HOST" ] && [ -n "$WEB_PORT" ] && [ -n "$CORE_URL" ] || fail "Cannot capture legacy install profile: Web topology is incomplete"
  else
    INSTALL_WEB=off
  fi
}

write_install_profile_value() {
  profile_key="$1"
  profile_value="$2"
  profile_escaped="$(printf '%s' "$profile_value" | sed "s/'/'\\\\''/g")"
  printf "%s='%s'\n" "$profile_key" "$profile_escaped"
}

write_install_profile() {
  tmp_profile="$(mktemp)"
  if install_web_enabled; then profile_install_web=on; else profile_install_web=off; fi
  {
    printf '# Managed by GP Access Control Plane installer. Reconfigure only through the explicit installer.\n'
    write_install_profile_value GP_INSTALL_USER "$TARGET_USER"
    write_install_profile_value GP_INSTALL_DIR "$INSTALL_DIR"
    write_install_profile_value GP_STATE_DIR "$STATE_DIR"
    write_install_profile_value GP_SERVICE_NAME "$SERVICE_NAME"
    write_install_profile_value GP_CORE_SERVICE_NAME "$CORE_SERVICE_NAME"
    write_install_profile_value GP_INSTALL_WEB "$profile_install_web"
    write_install_profile_value GP_WEB_HOST "$WEB_HOST"
    write_install_profile_value GP_WEB_PORT "$WEB_PORT"
    write_install_profile_value GP_WEB_ENV_FILE "$WEB_ENV_FILE"
    write_install_profile_value GP_CORE_HOST "$CORE_HOST"
    write_install_profile_value GP_CORE_PORT "$CORE_PORT"
    write_install_profile_value GP_CORE_URL "$CORE_URL"
    write_install_profile_value GP_CORE_ENV_FILE "$CORE_ENV_FILE"
    write_install_profile_value GP_ZAPRET_DIR "$ZAPRET_DIR"
    write_install_profile_value GP_ROOT_HELPER_PATH "$ROOT_HELPER_PATH"
    write_install_profile_value GP_ROOT_HELPER_CONFIG "$ROOT_HELPER_CONFIG"
    write_install_profile_value GP_ROOT_HELPER_RUN_DIR "$ROOT_HELPER_RUN_DIR"
    write_install_profile_value GP_SUDOERS_PATH "$SUDOERS_PATH"
    write_install_profile_value GP_SERVICE_MEMORY_HIGH "$SERVICE_MEMORY_HIGH"
    write_install_profile_value GP_SERVICE_MEMORY_MAX "$SERVICE_MEMORY_MAX"
  } > "$tmp_profile"
  as_root install -m 0640 -o root -g root "$tmp_profile" "$INSTALL_PROFILE"
  rm -f "$tmp_profile"
}

if [ "$LEGACY_PROFILE_CAPTURE" = on ]; then
  capture_legacy_install_profile
fi

log "Installing for user: $TARGET_USER"
log "Install directory: $INSTALL_DIR"
log "State directory: $STATE_DIR"

if step_log packages "Updating package index and installing required packages"; then
  as_root apt-get update
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
fi

if step_log zapret "Installing zapret2"; then
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
    install_luajit_dev_package
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
fi

if step_log app "Installing GP Access Control Plane"; then
  resolve_install_ref
  run_as_target mkdir -p "$(dirname "$INSTALL_DIR")"
  if [ -d "$INSTALL_DIR/.git" ]; then
    if [ -n "$(repo_git status --short)" ]; then
      if force_clean_enabled; then
        log "Repository has local changes; release update will discard worktree changes before checkout"
        repo_git reset --hard
        repo_git clean -fd
      else
        fail "Repository has local changes: $INSTALL_DIR. Commit or remove them, then run installer again."
      fi
    fi
    repo_git fetch origin "$BRANCH" || true
    if repo_git rev-parse --verify --quiet "refs/remotes/origin/$BRANCH" >/dev/null; then
      repo_git checkout -B "$BRANCH" "origin/$BRANCH"
      if force_clean_enabled; then
        repo_git reset --hard "origin/$BRANCH"
      else
        repo_git pull --ff-only origin "$BRANCH"
      fi
    else
      repo_git fetch origin "+refs/tags/$BRANCH:refs/tags/$BRANCH" || true
      if ! repo_git rev-parse --verify --quiet "refs/tags/$BRANCH" >/dev/null; then
        fail "Cannot find branch or tag: $BRANCH"
      fi
      repo_git checkout --detach "$BRANCH"
      if force_clean_enabled; then
        repo_git reset --hard "$BRANCH"
      fi
    fi
  elif [ -e "$INSTALL_DIR" ]; then
    fail "Install path exists but is not a git repository: $INSTALL_DIR"
  else
    run_as_target git clone --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
  fi

  log "Creating Python virtual environment"
  run_as_target python3 -m venv "$INSTALL_DIR/.venv"
  run_as_target "$INSTALL_DIR/.venv/bin/python" -m pip install --upgrade pip setuptools wheel
  run_as_target "$INSTALL_DIR/.venv/bin/python" -m pip install -e "$INSTALL_DIR"
fi

if step_log v2fly "Preparing local v2fly domain catalog"; then
  if ! prepare_v2fly_local_catalog; then
    log "v2fly local catalog was not prepared; v2fly import will become available after the next successful install or update"
  fi
fi

if [ "$LEGACY_PROFILE_CAPTURE" = on ] && step_enabled root-helper; then
  log "[root-helper] skipped while capturing the existing install profile"
elif step_log root-helper "Installing GP root helper"; then
  as_root install -d -m 0755 "$(dirname "$ROOT_HELPER_PATH")"
  as_root install -m 0755 -o root -g root "$INSTALL_DIR/scripts/gp-root-helper.sh" "$ROOT_HELPER_PATH"

  TMP_ROOT_HELPER_CONFIG="$(mktemp)"
  ZAPRET_DIR_ESCAPED="$(printf '%s' "$ZAPRET_DIR" | sed "s/'/'\\\\''/g")"
  ROOT_HELPER_RUN_DIR_ESCAPED="$(printf '%s' "$ROOT_HELPER_RUN_DIR" | sed "s/'/'\\\\''/g")"
  {
    printf "ZAPRET_DIR='%s'\n" "$ZAPRET_DIR_ESCAPED"
    printf "GP_ROOT_HELPER_RUN_DIR='%s'\n" "$ROOT_HELPER_RUN_DIR_ESCAPED"
  } > "$TMP_ROOT_HELPER_CONFIG"
  as_root install -m 0644 -o root -g root "$TMP_ROOT_HELPER_CONFIG" "$ROOT_HELPER_CONFIG"
  rm -f "$TMP_ROOT_HELPER_CONFIG"

  TMP_SUDOERS="$(mktemp)"
  printf '# Managed by GP Access Control Plane installer\n%s ALL=(root) NOPASSWD: %s *\n' "$TARGET_USER" "$ROOT_HELPER_PATH" > "$TMP_SUDOERS"
  as_root visudo -cf "$TMP_SUDOERS"
  as_root install -m 0440 -o root -g root "$TMP_SUDOERS" "$SUDOERS_PATH"
  rm -f "$TMP_SUDOERS"
fi

if [ "$LEGACY_PROFILE_CAPTURE" = on ] && step_enabled service; then
  log "[service] skipped while capturing the existing install profile"
elif step_log service "Creating and starting systemd service"; then
  install_service_env_file "$CORE_ENV_FILE"
  install_systemd_service "$CORE_SERVICE_NAME" "GP Strategy Finder Core API" "core" "$CORE_HOST" "$CORE_PORT" "$CORE_ENV_FILE"
  as_root systemctl daemon-reload
  as_root systemctl enable "$CORE_SERVICE_NAME"
  as_root systemctl restart "$CORE_SERVICE_NAME"
  if install_web_enabled; then
    install_service_env_file "$WEB_ENV_FILE"
    install_systemd_service "$SERVICE_NAME" "GP Strategy Finder Web UI" "web" "$WEB_HOST" "$WEB_PORT" "$WEB_ENV_FILE" "--core-url $CORE_URL" "$CORE_SERVICE_NAME" "$CORE_SERVICE_NAME"
    as_root systemctl daemon-reload
    as_root systemctl enable "$SERVICE_NAME"
    as_root systemctl restart "$SERVICE_NAME"
  else
    as_root systemctl disable --now "$SERVICE_NAME" >/dev/null 2>&1 || true
  fi
fi

if step_log check "Checking installation"; then
  run_as_target env GP_INSTALL_DIR="$INSTALL_DIR" GP_STATE_DIR="$STATE_DIR" "$INSTALL_DIR/.venv/bin/gp-control-plane" zapret2 check-install || true
  as_root systemctl --no-pager --full status "$CORE_SERVICE_NAME" || true
  if install_web_enabled; then
    as_root systemctl --no-pager --full status "$SERVICE_NAME" || true
  fi
fi

if { [ "$LEGACY_PROFILE_CAPTURE" = on ] && step_enabled app; } || { ! release_update_enabled && step_enabled service; }; then
  log "Writing root-owned resolved install profile"
  write_install_profile
fi

IP_ADDRESS="$(hostname -I 2>/dev/null | awk '{print $1}')"
if install_web_enabled && [ -n "${IP_ADDRESS:-}" ]; then
  printf '\nDone. Open: http://%s:%s/\n' "$IP_ADDRESS" "$WEB_PORT"
elif install_web_enabled; then
  printf '\nDone. Open host address on port %s.\n' "$WEB_PORT"
else
  printf '\nDone. Core API: http://%s:%s/\n' "$CORE_HOST" "$CORE_PORT"
fi
