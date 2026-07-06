#!/bin/sh
set -eu

CONFIG_FILE="${GP_ROOT_HELPER_CONFIG:-/etc/default/gp-control-plane-root-helper}"
[ -r "$CONFIG_FILE" ] && . "$CONFIG_FILE"
ZAPRET_DIR="${ZAPRET_DIR:-/opt/zapret2}"

fail() {
  printf 'gp-root-helper: %s\n' "$1" >&2
  exit 126
}

require_root() {
  [ "$(id -u)" -eq 0 ] || fail "must be executed as root"
}

real_path() {
  readlink -f "$1" 2>/dev/null || printf '%s\n' "$1"
}

validate_signal() {
  case "$1" in
    TERM|KILL|INT|HUP) printf '%s\n' "$1" ;;
    *) fail "unsupported signal: $1" ;;
  esac
}

validate_pid() {
  case "$1" in
    ''|*[!0-9]*) fail "invalid pid: $1" ;;
    *) printf '%s\n' "$1" ;;
  esac
}

validate_env_assignment() {
  case "$1" in
    *=*) ;;
    *) fail "invalid env assignment" ;;
  esac
  key="${1%%=*}"
  case "$key" in
    BATCH|DOMAINS|IPVS|TEST|SKIP_DNSCHECK|SKIP_IPBLOCK|ENABLE_HTTP|ENABLE_HTTPS_TLS12|ENABLE_HTTPS_TLS13|ENABLE_HTTP3|SCANLEVEL|REPEATS|PARALLEL|CURL_MAX_TIME|CURL_MAX_TIME_QUIC|CURL_MAX_TIME_DOH|GP_MD_CURL_PARALLELISM|ZAPRET_BASE|ZAPRET_RW) ;;
    *) fail "unsupported env key: $key" ;;
  esac
}

validate_run_target() {
  [ "$#" -ge 1 ] || fail "run target is required"
  target="$(real_path "$1")"
  zapret_blockcheck="$(real_path "$ZAPRET_DIR/blockcheck2.sh")"
  case "$target" in
    "$zapret_blockcheck"|/tmp/*/gp-multidomain-blockcheck.sh|/var/tmp/*/gp-multidomain-blockcheck.sh) ;;
    *) fail "unsupported run target: $1" ;;
  esac
  [ -x "$target" ] || fail "run target is not executable: $target"
  printf '%s\n' "$target"
}

validate_update_ref() {
  ref="${1:-}"
  [ -n "$ref" ] || fail "release ref is required"
  case "$ref" in
    *..*|/*|*\\*|*[!A-Za-z0-9._/-]*) fail "unsupported release ref: $ref" ;;
    *) printf '%s\n' "$ref" ;;
  esac
}

validate_install_dir() {
  [ "$#" -ge 1 ] || fail "install directory is required"
  install_dir="$(real_path "$1")"
  [ -d "$install_dir/.git" ] || fail "install directory is not a git repository: $install_dir"
  [ -x "$install_dir/scripts/install-raspberry-pi.sh" ] || fail "installer is not executable: $install_dir/scripts/install-raspberry-pi.sh"
  printf '%s\n' "$install_dir"
}

shell_quote() {
  printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}

run_target() {
  target="$(validate_run_target "$@")"
  shift
  exec "$target" "$@"
}

queue_update() {
  install_dir="$(validate_install_dir "$1")"
  ref="$(validate_update_ref "$2")"
  service_name="${GP_SERVICE_NAME:-gp-control-plane-web.service}"
  case "$service_name" in
    gp-control-plane-web.service|gp-control-plane-web@*.service) ;;
    *) fail "unsupported service name: $service_name" ;;
  esac

  log_dir="$install_dir/build/state/release-updates"
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  unit="gp-control-plane-update-$stamp"
  script="$log_dir/$unit.sh"
  log_file="$log_dir/$unit.log"
  install_user="$(stat -c '%U' "$install_dir" 2>/dev/null || printf '%s\n' '')"
  [ -n "$install_user" ] && [ "$install_user" != "UNKNOWN" ] || fail "cannot detect install directory owner: $install_dir"
  repo_url="$(git -c safe.directory="$install_dir" -C "$install_dir" remote get-url origin 2>/dev/null || printf '%s\n' 'https://github.com/balbomush/GP-access-control-plane.git')"

  mkdir -p "$log_dir"
  cat > "$script" <<SCRIPT
#!/bin/sh
set -eu
umask 022
exec > $(shell_quote "$log_file") 2>&1
echo "gp-control-plane update queued at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "install_dir=$(shell_quote "$install_dir")"
echo "ref=$(shell_quote "$ref")"
export GP_INSTALL_DIR=$(shell_quote "$install_dir")
export GP_INSTALL_USER=$(shell_quote "$install_user")
export GP_BRANCH=$(shell_quote "$ref")
export GP_REPO_URL=$(shell_quote "$repo_url")
export GP_SERVICE_NAME=$(shell_quote "$service_name")
if bash $(shell_quote "$install_dir/scripts/install-raspberry-pi.sh"); then
  installed_ref="\$(git -c safe.directory=$(shell_quote "$install_dir") -C $(shell_quote "$install_dir") describe --tags --exact-match 2>/dev/null || git -c safe.directory=$(shell_quote "$install_dir") -C $(shell_quote "$install_dir") rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
  installed_commit="\$(git -c safe.directory=$(shell_quote "$install_dir") -C $(shell_quote "$install_dir") rev-parse --short HEAD 2>/dev/null || true)"
  installed_version="\$($(shell_quote "$install_dir/.venv/bin/gp-control-plane") --version 2>/dev/null | awk '{print \$NF}' || true)"
  expected_version="\$(printf '%s' $(shell_quote "$ref") | sed 's/^v//')"
  echo "installed_ref=\$installed_ref"
  echo "installed_commit=\$installed_commit"
  echo "installed_version=\$installed_version"
  if [ "\$installed_ref" = $(shell_quote "$ref") ] || [ "\$installed_version" = "\$expected_version" ]; then
    echo "status=success"
  else
    echo "status=failed"
    echo "error=installed ref/version does not match requested release"
    exit 127
  fi
else
  code="\$?"
  echo "status=failed"
  echo "error=installer failed with code \$code"
  exit "\$code"
fi
SCRIPT
  chmod 0700 "$script"

  if command -v systemd-run >/dev/null 2>&1; then
    systemd-run --unit="$unit" --collect --property=Type=oneshot /bin/sh "$script" >/dev/null
  else
    nohup /bin/sh "$script" >/dev/null 2>&1 &
  fi
  printf 'queued=true\nunit=%s\nlog=%s\n' "$unit" "$log_file"
}

require_root

command="${1:-}"
[ -n "$command" ] || fail "command is required"
shift

case "$command" in
  check)
    exit 0
    ;;
  run)
    run_target "$@"
    ;;
  run-env)
    while [ "$#" -gt 0 ]; do
      if [ "$1" = "--" ]; then
        shift
        break
      fi
      validate_env_assignment "$1"
      export "$1"
      shift
    done
    run_target "$@"
    ;;
  queue-update)
    [ "$#" -eq 2 ] || fail "queue-update requires install directory and release ref"
    queue_update "$@"
    ;;
  kill)
    signal="$(validate_signal "${1:-}")"
    shift
    [ "$#" -gt 0 ] || exit 0
    for pid in "$@"; do
      validate_pid "$pid" >/dev/null
      kill "-$signal" "$pid" 2>/dev/null || true
    done
    ;;
  killpg)
    signal="$(validate_signal "${1:-}")"
    pgid="$(validate_pid "${2:-}")"
    kill "-$signal" "-$pgid" 2>/dev/null || true
    ;;
  nft-list-tables)
    exec nft list tables
    ;;
  nft-delete-blockcheck-table)
    family="${1:-}"
    table="${2:-}"
    case "$family" in
      ip|ip6|inet|arp|bridge|netdev) ;;
      *) fail "unsupported nft family: $family" ;;
    esac
    case "$table" in
      blockcheck[0-9]*) ;;
      *) fail "unsupported nft table: $table" ;;
    esac
    exec nft delete table "$family" "$table"
    ;;
  *)
    fail "unsupported command: $command"
    ;;
esac
