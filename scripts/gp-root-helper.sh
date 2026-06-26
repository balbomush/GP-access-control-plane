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
    BATCH|DOMAINS|IPVS|TEST|SKIP_DNSCHECK|SKIP_IPBLOCK|ENABLE_HTTP|ENABLE_HTTPS_TLS12|ENABLE_HTTPS_TLS13|ENABLE_HTTP3|SCANLEVEL|REPEATS|PARALLEL|GP_MD_CURL_PARALLELISM|ZAPRET_BASE|ZAPRET_RW) ;;
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

run_target() {
  target="$(validate_run_target "$@")"
  shift
  exec "$target" "$@"
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
