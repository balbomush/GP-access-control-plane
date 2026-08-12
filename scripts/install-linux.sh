#!/usr/bin/env bash
set -Eeuo pipefail

INSTALL_FORCE_CLEAN="${GP_INSTALL_FORCE_CLEAN:-off}"
# Fixed, root-owned profile; a release update must not use a caller-selected path.
INSTALL_PROFILE="/etc/default/gp-control-plane-install-profile"
LEGACY_PROFILE_CAPTURE=off
TRUSTED_SOURCE_DIR="${GP_TRUSTED_SOURCE_DIR:-}"
STRICT_PREFLIGHT=off
SCRIPT_PATH="$(readlink -f -- "${BASH_SOURCE[0]}")"
SCRIPT_SOURCE_DIR="$(cd -- "$(dirname -- "$SCRIPT_PATH")/.." && pwd -P)"

release_update_enabled() {
  case "$INSTALL_FORCE_CLEAN" in
    1|true|TRUE|yes|YES|on|ON) return 0 ;;
    *) return 1 ;;
  esac
}

strict_update_requested() {
  [ -n "${GP_TRUSTED_SOURCE_DIR:-}" ] || [ -n "${GP_UPDATE_CANDIDATE_REF:-}" ] || [ -n "${GP_UPDATE_EXPECTED_SHA:-}" ]
}

validate_install_profile_file() {
  [ -e "$INSTALL_PROFILE" ] || [ -L "$INSTALL_PROFILE" ] || { printf '\nERROR: strict update requires install profile: %s\n' "$INSTALL_PROFILE" >&2; return 1; }
  [ -f "$INSTALL_PROFILE" ] && [ ! -L "$INSTALL_PROFILE" ] || { printf '\nERROR: install profile is not a regular file: %s\n' "$INSTALL_PROFILE" >&2; return 1; }
  [ "$(stat -c '%u:%g:%a' "$INSTALL_PROFILE" 2>/dev/null || true)" = "0:0:600" ] || { printf '\nERROR: install profile must be root:root mode 0600: %s\n' "$INSTALL_PROFILE" >&2; return 1; }
}

read_trusted_env_value() {
  trusted_env_file="$1"
  trusted_env_key="$2"

  if [ "$(id -u)" -eq 0 ]; then
    trusted_env_reader=(awk)
  else
    trusted_env_reader=(sudo awk)
  fi

  "${trusted_env_reader[@]}" -v key="$trusted_env_key" '
    function decode_single_quoted(value,    length_value, position, character, decoded) {
      length_value = length(value)
      if (length_value < 2 || substr(value, 1, 1) != "\047") return ""

      for (position = 2; position <= length_value; position++) {
        character = substr(value, position, 1)
        if (character == "\047") {
          if (position == length_value) {
            decoded_value = decoded
            return "ok"
          }
          if (substr(value, position + 1, 3) != "\\\047\047") return ""
          decoded = decoded "\047"
          position += 3
        } else {
          decoded = decoded character
        }
      }
      return ""
    }

    {
      sub(/\r$/, "")
      if (index($0, key "=") != 1) next
      matches++
      if (decode_single_quoted(substr($0, length(key) + 2)) != "ok") invalid = 1
    }

    END {
      if (invalid || matches > 1) exit 2
      if (matches == 0) exit 3
      print decoded_value
    }
  ' "$trusted_env_file"
}

load_trusted_env_values() {
  trusted_env_file="$1"
  shift

  for trusted_env_key in "$@"; do
    case "$trusted_env_key" in
      GP_INSTALL_USER|GP_INSTALL_DIR|GP_STATE_DIR|GP_SERVICE_NAME|GP_CORE_SERVICE_NAME|GP_INSTALL_WEB|GP_WEB_HOST|GP_WEB_PORT|GP_WEB_ENV_FILE|GP_CORE_HOST|GP_CORE_PORT|GP_CORE_URL|GP_CORE_ENV_FILE|GP_ZAPRET_DIR|GP_ROOT_HELPER_PATH|GP_ROOT_HELPER_CONFIG|GP_ROOT_HELPER_RUN_DIR|GP_SUDOERS_PATH|GP_SERVICE_MEMORY_HIGH|GP_SERVICE_MEMORY_MAX) ;;
      *) return 2 ;;
    esac

    if trusted_env_value="$(read_trusted_env_value "$trusted_env_file" "$trusted_env_key")"; then
      printf -v "$trusted_env_key" '%s' "$trusted_env_value"
      export "$trusted_env_key"
    else
      trusted_env_status=$?
      [ "$trusted_env_status" -eq 3 ] || return "$trusted_env_status"
    fi
  done
}

if strict_update_requested; then
  validate_install_profile_file || exit 1
  load_trusted_env_values "$INSTALL_PROFILE" \
    GP_INSTALL_USER GP_INSTALL_DIR GP_STATE_DIR GP_SERVICE_NAME GP_CORE_SERVICE_NAME GP_INSTALL_WEB \
    GP_WEB_HOST GP_WEB_PORT GP_WEB_ENV_FILE GP_CORE_HOST GP_CORE_PORT GP_CORE_URL GP_CORE_ENV_FILE \
    GP_ZAPRET_DIR GP_ROOT_HELPER_PATH GP_ROOT_HELPER_CONFIG GP_ROOT_HELPER_RUN_DIR GP_SUDOERS_PATH \
    GP_SERVICE_MEMORY_HIGH GP_SERVICE_MEMORY_MAX \
    || { printf '\nERROR: cannot safely parse install profile: %s\n' "$INSTALL_PROFILE" >&2; exit 1; }
elif release_update_enabled; then
  if [ -e "$INSTALL_PROFILE" ] || [ -L "$INSTALL_PROFILE" ]; then
    [ -f "$INSTALL_PROFILE" ] && [ ! -L "$INSTALL_PROFILE" ] || { printf '\nERROR: install profile is not a regular file: %s\n' "$INSTALL_PROFILE" >&2; exit 1; }
    profile_uid="$(stat -c '%u' "$INSTALL_PROFILE" 2>/dev/null || true)"
    profile_gid="$(stat -c '%g' "$INSTALL_PROFILE" 2>/dev/null || true)"
    profile_mode="$(stat -c '%a' "$INSTALL_PROFILE" 2>/dev/null || true)"
    [ "$profile_uid" = "0" ] && [ "$profile_gid" = "0" ] && [ "$profile_mode" = "600" ] || { printf '\nERROR: install profile must be root:root mode 0600: %s\n' "$INSTALL_PROFILE" >&2; exit 1; }
    load_trusted_env_values "$INSTALL_PROFILE" \
      GP_INSTALL_USER GP_INSTALL_DIR GP_STATE_DIR GP_SERVICE_NAME GP_CORE_SERVICE_NAME GP_INSTALL_WEB \
      GP_WEB_HOST GP_WEB_PORT GP_WEB_ENV_FILE GP_CORE_HOST GP_CORE_PORT GP_CORE_URL GP_CORE_ENV_FILE \
      GP_ZAPRET_DIR GP_ROOT_HELPER_PATH GP_ROOT_HELPER_CONFIG GP_ROOT_HELPER_RUN_DIR GP_SUDOERS_PATH \
      GP_SERVICE_MEMORY_HIGH GP_SERVICE_MEMORY_MAX \
      || { printf '\nERROR: cannot safely parse install profile: %s\n' "$INSTALL_PROFILE" >&2; exit 1; }
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
UPDATE_CANDIDATE_REF="${GP_UPDATE_CANDIDATE_REF:-}"
UPDATE_EXPECTED_SHA="${GP_UPDATE_EXPECTED_SHA:-}"
PINNED_UPDATE=off

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
    --strict-preflight)
      STRICT_PREFLIGHT=on
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

pinned_update_enabled() {
  [ "$PINNED_UPDATE" = on ]
}

validate_pinned_update_inputs() {
  if [ -z "$TRUSTED_SOURCE_DIR" ] && [ -z "$UPDATE_CANDIDATE_REF" ] && [ -z "$UPDATE_EXPECTED_SHA" ]; then
    return 0
  fi

  PINNED_UPDATE=on
  [ -n "$TRUSTED_SOURCE_DIR" ] && [ -n "$UPDATE_CANDIDATE_REF" ] && [ -n "$UPDATE_EXPECTED_SHA" ] \
    || fail "Strict trusted update requires GP_TRUSTED_SOURCE_DIR, GP_UPDATE_CANDIDATE_REF and GP_UPDATE_EXPECTED_SHA together."
  force_clean_enabled \
    || fail "Strict trusted update requires GP_INSTALL_FORCE_CLEAN=on."
  case "$TRUSTED_SOURCE_DIR" in
    /*) ;;
    *) fail "Strict trusted update source must be an absolute path: $TRUSTED_SOURCE_DIR" ;;
  esac
  [ "$TRUSTED_SOURCE_DIR" != / ] || fail "Strict trusted update source must not be the filesystem root."
  case "$UPDATE_CANDIDATE_REF" in
    refs/tags/*) ;;
    *) fail "Strict trusted update ref must be a full refs/tags/* ref: $UPDATE_CANDIDATE_REF" ;;
  esac
  git check-ref-format "$UPDATE_CANDIDATE_REF" >/dev/null \
    || fail "Strict trusted update ref is not a valid Git ref: $UPDATE_CANDIDATE_REF"
  case "$UPDATE_EXPECTED_SHA" in
    *[!0-9a-f]*|'') fail "Strict trusted update SHA must be exactly 40 lowercase hexadecimal characters." ;;
  esac
  [ "${#UPDATE_EXPECTED_SHA}" -eq 40 ] \
    || fail "Strict trusted update SHA must be exactly 40 lowercase hexadecimal characters."
}

trusted_source_directory() {
  trusted_directory="$1"
  [ -d "$trusted_directory" ] && [ ! -L "$trusted_directory" ] \
    || fail "Strict trusted update source directory is not a non-symlink directory: $trusted_directory"
  [ "$(as_root stat -c '%u:%g' "$trusted_directory" 2>/dev/null || true)" = "0:0" ] \
    || fail "Strict trusted update source directory must be owned by root:root: $trusted_directory"
  trusted_directory_mode="$(as_root stat -c '%a' "$trusted_directory" 2>/dev/null || true)"
  [ -n "$trusted_directory_mode" ] && [ $((8#$trusted_directory_mode & 022)) -eq 0 ] \
    || fail "Strict trusted update source directory must not be group/world-writable: $trusted_directory"
}

trusted_source_file() {
  trusted_file="$1"
  [ -f "$trusted_file" ] && [ ! -L "$trusted_file" ] \
    || fail "Strict trusted update source file is not a regular file: $trusted_file"
  [ "$(as_root stat -c '%u:%g' "$trusted_file" 2>/dev/null || true)" = "0:0" ] \
    || fail "Strict trusted update source file must be owned by root:root: $trusted_file"
  trusted_file_mode="$(as_root stat -c '%a' "$trusted_file" 2>/dev/null || true)"
  [ -n "$trusted_file_mode" ] && [ $((8#$trusted_file_mode & 022)) -eq 0 ] \
    || fail "Strict trusted update source file must not be group/world-writable: $trusted_file"
}

verify_pinned_update_checkout() {
  pinned_update_enabled || return 0
  trusted_source_resolved="$(as_root readlink -f -- "$TRUSTED_SOURCE_DIR" 2>/dev/null || true)"
  [ "$trusted_source_resolved" = "$TRUSTED_SOURCE_DIR" ] \
    || fail "Strict trusted update source must be a canonical non-symlink path: $TRUSTED_SOURCE_DIR"
  [ "$SCRIPT_SOURCE_DIR" = "$TRUSTED_SOURCE_DIR" ] \
    || fail "Strict trusted update installer must execute from GP_TRUSTED_SOURCE_DIR: $TRUSTED_SOURCE_DIR"
  trusted_source_directory "$TRUSTED_SOURCE_DIR"
  trusted_source_directory "$TRUSTED_SOURCE_DIR/scripts"
  trusted_source_file "$SCRIPT_PATH"
  [ "$(as_root git -c safe.directory="$TRUSTED_SOURCE_DIR" -C "$TRUSTED_SOURCE_DIR" rev-parse --is-inside-work-tree 2>/dev/null || true)" = true ] \
    || fail "Strict trusted update source is not a Git worktree: $TRUSTED_SOURCE_DIR"
  actual_pinned_sha="$(as_root git -c safe.directory="$TRUSTED_SOURCE_DIR" -C "$TRUSTED_SOURCE_DIR" rev-parse --verify --quiet 'HEAD^{commit}' 2>/dev/null || true)"
  [ "$actual_pinned_sha" = "$UPDATE_EXPECTED_SHA" ] \
    || fail "Strict trusted update source mismatch: expected $UPDATE_EXPECTED_SHA, found ${actual_pinned_sha:-unavailable}. Refusing privileged actions."
  log "Strict trusted update: using staged source $UPDATE_CANDIDATE_REF at $UPDATE_EXPECTED_SHA"
}

trusted_project_file() {
  project_relative_path="$1"
  project_source_path="$TRUSTED_SOURCE_DIR/$project_relative_path"
  trusted_source_file "$project_source_path"
  printf '%s\n' "$project_source_path"
}

ensure_root_directory() {
  managed_directory="$1"
  managed_mode="$2"
  managed_owner="$3"
  managed_group="$4"
  expected_mode="$(printf '%o' "$((8#$managed_mode))")"

  if as_root test -e "$managed_directory" || as_root test -L "$managed_directory"; then
    as_root test -d "$managed_directory" && ! as_root test -L "$managed_directory" \
      || fail "Managed directory must be a non-symlink directory: $managed_directory"
  fi
  as_root install -d -m "$managed_mode" -o "$managed_owner" -g "$managed_group" "$managed_directory"
  as_root test -d "$managed_directory" && ! as_root test -L "$managed_directory" \
    || fail "Managed directory was not created safely: $managed_directory"
  [ "$(as_root stat -c '%u:%g:%a' "$managed_directory" 2>/dev/null || true)" = "0:$(getent group "$managed_group" | cut -d: -f3):$expected_mode" ] \
    || fail "Managed directory has unexpected ownership or mode: $managed_directory"
}

ensure_root_regular_file() {
  managed_file="$1"
  managed_mode="$2"
  managed_owner="$3"
  managed_group="$4"
  expected_mode="$(printf '%o' "$((8#$managed_mode))")"

  if as_root test -e "$managed_file" || as_root test -L "$managed_file"; then
    as_root test -f "$managed_file" && ! as_root test -L "$managed_file" \
      || fail "Managed file must be a regular non-symlink file: $managed_file"
  else
    as_root touch "$managed_file"
  fi
  as_root chown "$managed_owner:$managed_group" "$managed_file"
  as_root chmod "$managed_mode" "$managed_file"
  as_root test -f "$managed_file" && ! as_root test -L "$managed_file" \
    && [ "$(as_root stat -c '%u:%g:%a' "$managed_file" 2>/dev/null || true)" = "0:$(getent group "$managed_group" | cut -d: -f3):$expected_mode" ] \
    || fail "Managed file has unexpected ownership or mode: $managed_file"
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
default_state_dir() {
  state_install_parent="$(dirname -- "$INSTALL_DIR")"
  state_install_base="$(basename -- "$INSTALL_DIR")"
  printf '%s/.%s.data/state\n' "$state_install_parent" "$state_install_base"
}

# A manual reinstall of a legacy worktree must not silently strand its state.
# Strict release updates get a separate, explicitly guarded migration below.
if [ -n "${GP_STATE_DIR:-}" ]; then
  STATE_DIR="$GP_STATE_DIR"
elif ! strict_update_requested && [ -d "$INSTALL_DIR/build/state" ] && [ ! -L "$INSTALL_DIR/build/state" ]; then
  STATE_DIR="$INSTALL_DIR/build/state"
else
  STATE_DIR="$(default_state_dir)"
fi
TARGET_BIN_DIR="$TARGET_HOME/.local/bin"
SERVICE_PATH="$INSTALL_DIR/.venv/bin:$TARGET_BIN_DIR:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

as_root() {
  if [ "$CURRENT_UID" -eq 0 ]; then
    "$@"
  else
    sudo "$@"
  fi
}

# A normal/manual installation may deliberately use alternate destinations.
# A strict release update, however, runs staged code with root privileges and
# must never turn a root-owned profile into a generic privileged write plan.
strict_require_fixed_privileged_path() {
  strict_path_name="$1"
  strict_path_value="$2"
  strict_path_expected="$3"
  [ "$strict_path_value" = "$strict_path_expected" ] \
    || fail "Strict trusted update requires $strict_path_name=$strict_path_expected."
}

strict_safe_root_parent_chain() {
  strict_parent="$1"
  strict_label="$2"
  while :; do
    as_root test -d "$strict_parent" && ! as_root test -L "$strict_parent" \
      || fail "Strict trusted update $strict_label has a missing or symlinked parent: $strict_parent"
    strict_parent_resolved="$(as_root readlink -f -- "$strict_parent" 2>/dev/null || true)"
    [ "$strict_parent_resolved" = "$strict_parent" ] \
      || fail "Strict trusted update $strict_label has a non-canonical parent: $strict_parent"
    strict_parent_uid="$(as_root stat -c '%u' "$strict_parent" 2>/dev/null || true)"
    strict_parent_mode="$(as_root stat -c '%A' "$strict_parent" 2>/dev/null || true)"
    case "$strict_parent_mode" in ?????w*|????????w*) strict_parent_writable=1 ;; *) strict_parent_writable=0 ;; esac
    [ "$strict_parent_uid" = 0 ] && [ "$strict_parent_writable" = 0 ] \
      || fail "Strict trusted update $strict_label parent must be root-owned and not group/world-writable: $strict_parent"
    [ "$strict_parent" = / ] && return 0
    strict_parent="$(dirname -- "$strict_parent")"
  done
}

strict_safe_root_target() {
  strict_target="$1"
  strict_target_kind="$2"
  strict_target_label="$3"
  strict_safe_root_parent_chain "$(dirname -- "$strict_target")" "$strict_target_label"
  if as_root test -e "$strict_target" || as_root test -L "$strict_target"; then
    case "$strict_target_kind" in
      file) as_root test -f "$strict_target" && ! as_root test -L "$strict_target" ;;
      directory) as_root test -d "$strict_target" && ! as_root test -L "$strict_target" ;;
      *) return 2 ;;
    esac || fail "Strict trusted update $strict_target_label must be a regular non-symlink $strict_target_kind: $strict_target"
    strict_target_resolved="$(as_root readlink -f -- "$strict_target" 2>/dev/null || true)"
    [ "$strict_target_resolved" = "$strict_target" ] \
      || fail "Strict trusted update $strict_target_label must be canonical: $strict_target"
    strict_target_uid="$(as_root stat -c '%u' "$strict_target" 2>/dev/null || true)"
    strict_target_mode="$(as_root stat -c '%A' "$strict_target" 2>/dev/null || true)"
    case "$strict_target_mode" in ?????w*|????????w*) strict_target_writable=1 ;; *) strict_target_writable=0 ;; esac
    [ "$strict_target_uid" = 0 ] && [ "$strict_target_writable" = 0 ] \
      || fail "Strict trusted update $strict_target_label must be root-owned and not group/world-writable: $strict_target"
  fi
}

validate_strict_privileged_destinations() {
  pinned_update_enabled || return 0
  strict_require_fixed_privileged_path GP_CORE_ENV_FILE "$CORE_ENV_FILE" /etc/default/gp-control-plane-core
  strict_require_fixed_privileged_path GP_WEB_ENV_FILE "$WEB_ENV_FILE" /etc/default/gp-control-plane-web
  strict_require_fixed_privileged_path GP_ROOT_HELPER_PATH "$ROOT_HELPER_PATH" /usr/local/libexec/gp-control-plane/gp-root-helper
  strict_require_fixed_privileged_path GP_ROOT_HELPER_CONFIG "$ROOT_HELPER_CONFIG" /etc/default/gp-control-plane-root-helper
  strict_require_fixed_privileged_path GP_ROOT_HELPER_RUN_DIR "$ROOT_HELPER_RUN_DIR" /run/gp-control-plane/runs
  strict_require_fixed_privileged_path GP_SUDOERS_PATH "$SUDOERS_PATH" /etc/sudoers.d/gp-control-plane-root-helper
  strict_require_fixed_privileged_path GP_ZAPRET_DIR "$ZAPRET_DIR" /opt/zapret2
  [ "$CORE_SERVICE_NAME" = gp-control-plane-core.service ] \
    || fail "Strict trusted update requires GP_CORE_SERVICE_NAME=gp-control-plane-core.service."
  [ "$SERVICE_NAME" = gp-control-plane-web.service ] \
    || fail "Strict trusted update requires GP_SERVICE_NAME=gp-control-plane-web.service."
  [ "$TARGET_USER" != root ] \
    || fail "Strict trusted update requires a non-root GP_INSTALL_USER."

  strict_safe_root_target "$INSTALL_PROFILE" file install-profile
  strict_safe_root_target "$CORE_ENV_FILE" file GP_CORE_ENV_FILE
  strict_safe_root_target "$WEB_ENV_FILE" file GP_WEB_ENV_FILE
  strict_safe_root_target "$ROOT_HELPER_PATH" file GP_ROOT_HELPER_PATH
  strict_safe_root_target "$ROOT_HELPER_CONFIG" file GP_ROOT_HELPER_CONFIG
  strict_safe_root_target "$ROOT_HELPER_RUN_DIR" directory GP_ROOT_HELPER_RUN_DIR
  strict_safe_root_target "$SUDOERS_PATH" file GP_SUDOERS_PATH
  strict_safe_root_target "$ZAPRET_DIR" directory GP_ZAPRET_DIR
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

strict_migrate_internal_state() {
  [ "${GP_STRICT_STATE_MIGRATION:-off}" = on ] || return 0
  pinned_update_enabled || fail "State migration is only permitted during a strict trusted update."

  migration_source="${GP_STRICT_STATE_MIGRATION_SOURCE:-}"
  migration_root="${GP_STRICT_STATE_MIGRATION_ROOT:-}"
  [ -n "$migration_source" ] && [ -n "$migration_root" ] || fail "Strict state migration metadata is incomplete."
  case "$migration_source" in /*) ;; *) fail "Strict state migration source must be absolute." ;; esac
  case "$migration_root" in /*) ;; *) fail "Strict state migration target must be absolute." ;; esac

  install_resolved="$(readlink -f -- "$INSTALL_DIR" 2>/dev/null || true)"
  [ "$install_resolved" = "$INSTALL_DIR" ] && [ -d "$INSTALL_DIR" ] && [ ! -L "$INSTALL_DIR" ] \
    || fail "Strict state migration install directory is not canonical and safe: $INSTALL_DIR"
  expected_root="$(dirname -- "$INSTALL_DIR")/.$(basename -- "$INSTALL_DIR").data"
  [ "$migration_root" = "$expected_root" ] || fail "Strict state migration target does not match the installation sibling data root."
  [ ! -e "$migration_root" ] && [ ! -L "$migration_root" ] || fail "Strict state migration target already exists: $migration_root"

  migration_source_resolved="$(readlink -f -- "$migration_source" 2>/dev/null || true)"
  [ "$migration_source_resolved" = "$migration_source" ] && [ -d "$migration_source" ] && [ ! -L "$migration_source" ] \
    || fail "Strict state migration source is not a canonical non-symlink directory: $migration_source"
  migration_previous="$(dirname -- "$INSTALL_DIR")/.$(basename -- "$INSTALL_DIR").strict-previous"
  case "$migration_source" in "$migration_previous"/*) ;; *) fail "Strict state migration source is outside the preserved previous worktree." ;; esac
  migration_source_parent="$(dirname -- "$migration_source")"
  migration_backups="$migration_source_parent/backups"
  if [ -e "$migration_backups" ] || [ -L "$migration_backups" ]; then
    migration_backups_resolved="$(readlink -f -- "$migration_backups" 2>/dev/null || true)"
    [ "$migration_backups_resolved" = "$migration_backups" ] && [ -d "$migration_backups" ] && [ ! -L "$migration_backups" ] \
      || fail "Strict state migration backups directory is not a canonical non-symlink directory: $migration_backups"
  fi

  migration_stage="$migration_root.strict-stage-$$"
  [ ! -e "$migration_stage" ] && [ ! -L "$migration_stage" ] || fail "Strict state migration staging path already exists: $migration_stage"
  log "Strict update: quiescing services before persistent-state migration"
  if install_web_enabled; then as_root systemctl stop "$SERVICE_NAME"; fi
  as_root systemctl stop "$CORE_SERVICE_NAME"

  run_as_target install -d -m 0700 "$migration_stage"
  run_as_target cp -a -- "$migration_source" "$migration_stage/state"
  if [ -d "$migration_backups" ]; then run_as_target cp -a -- "$migration_backups" "$migration_stage/backups"; fi
  run_as_target mv -- "$migration_stage" "$migration_root"
  [ -d "$migration_root/state" ] && [ ! -L "$migration_root/state" ] || fail "Strict state migration did not publish a safe state directory."
  STATE_DIR="$migration_root/state"
  printf 'state_layout=migrated\nstate_migration=completed\n'
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
KillMode=control-group
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
  service_env_file="$1"
  trusted_root_file "$service_env_file" || fail "Cannot safely read service environment: $service_env_file"

  # The legacy EnvironmentFile is root-owned and can be 0640, so read only the
  # values this installer needs through sudo.  Do not source it: EnvironmentFile
  # contents are data, not installer shell code.
  load_trusted_env_values "$service_env_file" GP_INSTALL_DIR GP_STATE_DIR GP_INSTALL_WEB \
    || fail "Cannot safely parse service environment: $service_env_file"
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
  install_profile_dir="$(dirname -- "$INSTALL_PROFILE")"
  as_root install -d -m 0755 -o root -g root "$install_profile_dir"
  if as_root test -e "$INSTALL_PROFILE" || as_root test -L "$INSTALL_PROFILE"; then
    as_root test -f "$INSTALL_PROFILE" && ! as_root test -L "$INSTALL_PROFILE" \
      || fail "Cannot replace non-regular install profile: $INSTALL_PROFILE"
  fi
  as_root install -m 0600 -o root -g root "$tmp_profile" "$INSTALL_PROFILE"
  as_root test -f "$INSTALL_PROFILE" && ! as_root test -L "$INSTALL_PROFILE" \
    && [ "$(as_root stat -c '%u:%g:%a' "$INSTALL_PROFILE" 2>/dev/null || true)" = "0:0:600" ] \
    || fail "Install profile was not created as root:root mode 0600: $INSTALL_PROFILE"
  rm -f "$tmp_profile"
}

if [ "$LEGACY_PROFILE_CAPTURE" = on ]; then
  capture_legacy_install_profile
fi

if [ -n "$TRUSTED_SOURCE_DIR" ] || [ -n "$UPDATE_CANDIDATE_REF" ] || [ -n "$UPDATE_EXPECTED_SHA" ]; then
  need_command git
fi
validate_pinned_update_inputs
validate_strict_privileged_destinations
verify_pinned_update_checkout

if [ "$STRICT_PREFLIGHT" = on ]; then
  log "Strict trusted update preflight passed for $UPDATE_CANDIDATE_REF at $UPDATE_EXPECTED_SHA"
  exit 0
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
  validate_strict_privileged_destinations
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
  if ! pinned_update_enabled; then
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
  fi

  log "Creating Python virtual environment"
  run_as_target python3 -m venv "$INSTALL_DIR/.venv"
  run_as_target "$INSTALL_DIR/.venv/bin/python" -m pip install --upgrade pip setuptools wheel
  run_as_target "$INSTALL_DIR/.venv/bin/python" -m pip install -e "$INSTALL_DIR"
fi

strict_migrate_internal_state

if step_log v2fly "Preparing local v2fly domain catalog"; then
  if ! prepare_v2fly_local_catalog; then
    log "v2fly local catalog was not prepared; v2fly import will become available after the next successful install or update"
  fi
fi

if [ "$LEGACY_PROFILE_CAPTURE" = on ] && step_enabled root-helper; then
  log "[root-helper] skipped while capturing the existing install profile"
elif step_log root-helper "Installing GP root helper"; then
  validate_strict_privileged_destinations
  as_root install -d -m 0755 "$(dirname "$ROOT_HELPER_PATH")"
  if pinned_update_enabled; then
    ROOT_HELPER_SOURCE="$(trusted_project_file scripts/gp-root-helper.sh)"
  else
    ROOT_HELPER_SOURCE="$INSTALL_DIR/scripts/gp-root-helper.sh"
  fi
  as_root install -m 0755 -o root -g root "$ROOT_HELPER_SOURCE" "$ROOT_HELPER_PATH"

  ensure_root_directory /run/gp-control-plane/gates 0700 root root
  ensure_root_regular_file /run/gp-control-plane/gates/discovery-update.lock 0600 root root
  ensure_root_directory /var/lib/gp-control-plane/release-gates 0750 root "$TARGET_GROUP"

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
  validate_strict_privileged_destinations
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

if { [ "$LEGACY_PROFILE_CAPTURE" = on ] && step_enabled app; } || { ! release_update_enabled && step_enabled service; } || { pinned_update_enabled && step_enabled service; }; then
  validate_strict_privileged_destinations
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
